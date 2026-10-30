# ============================================================================
# QBO Import Service — Pull data from QuickBooks Online into Slowbooks
#
# Import order follows the same dependency chain as IIF import:
#   accounts -> customers -> vendors -> items -> invoices -> payments
#
# Each entity type: query QBO -> check qbo_mappings for existing ->
# check by name/docnum for duplicates -> create or skip -> record mapping.
#
# QBO REST API returns Python objects via python-quickbooks SDK.
# All QBO object field access uses getattr(obj, field, None) for safety.
# ============================================================================

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from app.services.audit import audited_bulk_delete
from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.banking import BankAccount
from app.models.contacts import Customer, Vendor
from app.models.items import Item, ItemType
from app.models.jobs import Job
from app.models.invoices import Invoice, InvoiceLine, InvoiceStatus
from app.models.payments import Payment, PaymentAllocation
from app.models.qbo_mapping import QBOMapping
from app.models.transactions import Transaction
from app.schemas.common import TAX_RATE_PLACES
from app.services.qbo_common import (
    DELETED_IN_QBO,
    HERE,
    NOT_OWNED,
    QBO_TO_ACCOUNT_TYPE,
    VOIDED_IN_QBO,
    QBO_TO_ITEM_TYPE,
    create_mapping,
    apply_rollup_repair,
    get_mapping_by_qbo_id,
    is_journal_entry_type,
    journal_posting_matches,
    kept_here,
    ledger_posting,
    legacy_rollup_repair,
    rebase_account_balances,
)
from app.services.qbo_service import get_qbo_client
from app.services.safe_errors import DataProblem
from app.services import qbo_progress


def _field(obj, attr, default=None):
    """A QBO object's field. The SDK types most of what it reads, but not
    everything: a SalesReceipt's lines keep their SalesItemLineDetail (and
    its ItemRef) as plain dicts, so an attribute read found nothing there."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _safe(obj, attr, default=None):
    """Safe attribute access for QBO objects."""
    return _field(obj, attr, default) or default


def _safe_decimal(obj, attr) -> Decimal:
    """Safe decimal extraction from QBO object."""
    val = _field(obj, attr)
    if val is None:
        return Decimal("0")
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")


def _parse_qbo_date(s) -> date:
    """Parse QBO date string (YYYY-MM-DD) to date object."""
    if not s:
        return date.today()
    try:
        from datetime import datetime

        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return date.today()


class CustomerNotFound(DataProblem):
    error_code = "IMPORT_CUSTOMER_NOT_FOUND"


def _resolve_customer(db, ref):
    """QBO CustomerRef may refer to a subcustomer imported as a local job."""
    qbo_id = str(_safe(ref, "value", ""))
    name = _safe(ref, "name", "")
    customer_map = get_mapping_by_qbo_id(db, "customer", qbo_id) if qbo_id else None
    if customer_map:
        customer = db.get(Customer, customer_map.slowbooks_id)
        if customer:
            return customer.id, None
    job_map = get_mapping_by_qbo_id(db, "job", qbo_id) if qbo_id else None
    if job_map:
        job = db.get(Job, job_map.slowbooks_id)
        if job and db.get(Customer, job.customer_id):
            qbo_progress.emit(
                "resolve",
                f"CustomerRef QBO #{qbo_id} ({name}) resolved through local project #{job.id} to customer #{job.customer_id}",
            )
            return job.customer_id, job.id
    if name:
        customer = db.query(Customer).filter(Customer.name == name).first()
        if customer:
            return customer.id, None
    mapped = []
    if customer_map:
        mapped.append(
            f"mapped local customer #{customer_map.slowbooks_id} does not exist"
        )
    if job_map:
        job = db.get(Job, job_map.slowbooks_id)
        mapped.append(
            f"mapped local project #{job_map.slowbooks_id}"
            + (
                f" references missing customer #{job.customer_id}"
                if job
                else " does not exist"
            )
        )
    raise CustomerNotFound(
        f"Customer not found: CustomerRef QBO #{qbo_id or '(missing ID)'} ({name or '(missing name)'})"
        + (
            f"; {'; '.join(mapped)}"
            if mapped
            else "; no local customer or project mapping and no matching customer name"
        )
        + ". Import Customers before this document."
    )


def _all_qbo_objects(qbo_class, client):
    """The SDK's .all() returns one page of 100 unless positioned explicitly."""
    page_size = 100
    start_position = 1
    objects = []
    while True:
        qbo_progress.emit(
            "query",
            f"Fetching {getattr(qbo_class, 'qbo_object_name', qbo_class.__name__)} page at position {start_position}",
            item_id="",
        )
        page = qbo_class.all(
            qb=client, start_position=start_position, max_results=page_size
        )
        qbo_progress.emit(
            "fetch", f"Fetched {len(page)} source items", fetched=len(page), item_id=""
        )
        objects.extend(page)
        if len(page) < page_size:
            return objects
        start_position += len(page)


def _all_inactive_qbo_accounts(qbo_class, client):
    """QBO omits inactive accounts from its default account query."""
    page_size = 100
    start_position = 1
    objects = []
    while True:
        qbo_progress.emit(
            "query",
            f"Fetching inactive Accounts at position {start_position}",
            item_id="",
        )
        page = qbo_class.query(
            "SELECT * FROM Account WHERE Active = false "
            f"STARTPOSITION {start_position} MAXRESULTS {page_size}",
            qb=client,
        )
        qbo_progress.emit(
            "fetch",
            f"Fetched {len(page)} inactive accounts",
            fetched=len(page),
            item_id="",
        )
        objects.extend(page)
        if len(page) < page_size:
            return objects
        start_position += len(page)


# The QBO import's own postings. Anything else on an account was posted here.
_QBO_POSTINGS = ("qbo_ledger", "qbo_journal")


def _posted_here(db: Session, account_id: int) -> bool:
    """Does the account carry a posting from anything but the QBO import?"""
    from sqlalchemy import or_

    from app.models.transactions import TransactionLine

    return (
        db.query(TransactionLine.id)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .filter(
            TransactionLine.account_id == account_id,
            or_(
                Transaction.source_type.is_(None),
                Transaction.source_type.notin_(_QBO_POSTINGS),
            ),
        )
        .first()
        is not None
    )


def _sync_active(db: Session, account: Account, active: bool, qbo_id) -> None:
    """QBO's Active flag, for an account that carries nothing but the QBO
    import's postings. An account with postings made here is in use here:
    QBO switching off an account of the same name, or its own copy of it,
    does not take it out of the pickers (and QBO does not switch back on
    one switched off here)."""
    if account.is_active == active:
        return
    if _posted_here(db, account.id):
        qbo_progress.emit(
            "verify",
            f"Account QBO #{qbo_id} is {'active' if active else 'inactive'} in "
            f"QuickBooks Online; local account #{account.id} ({account.name}) "
            f"has postings made here, so it stays "
            f"{'active' if account.is_active else 'inactive'}",
            level="warning",
            code="IMPORT_ACCOUNT_ACTIVE_KEPT",
        )
        return
    account.is_active = active


def _sync_bank_identity(db: Session, account: Account, qbo_type: str) -> None:
    """Make QBO bank/card chart accounts visible and usable in Banking."""
    bank_kind = {"Bank": "bank", "Credit Card": "credit_card"}.get(qbo_type)
    if bank_kind is None:
        return
    expected_type = AccountType.ASSET if bank_kind == "bank" else AccountType.LIABILITY
    if account.account_type != expected_type:
        raise ValueError(
            f"QBO {qbo_type} account {account.name!r} matches a local account "
            "with an incompatible type"
        )
    if account.bank_kind is None:
        account.bank_kind = bank_kind
    elif account.bank_kind != bank_kind:
        raise ValueError(
            f"QBO {qbo_type} account {account.name!r} matches a different "
            "local banking kind"
        )
    if not db.query(BankAccount).filter(BankAccount.account_id == account.id).first():
        db.add(BankAccount(name=account.name, account_id=account.id))
        db.flush()


# ============================================================================
# Documents changed in QuickBooks Online after they were imported
# ============================================================================


_LABEL = {"invoice": "Invoice", "sales_receipt": "Sales receipt", "payment": "Payment"}


def _tax_code(detail) -> bool | None:
    """A QBO sales line's taxable flag, from its TaxCodeRef: "TAX" taxable,
    "NON" not (a US company's two codes); None when it has no code."""
    ref = _safe(detail, "TaxCodeRef")
    code = str(_safe(ref, "value", "") or "").strip().upper() if ref else ""
    return True if code == "TAX" else False if code == "NON" else None


# A document's tax rate is a fraction kept to six places (8.875% is 0.08875).
# a document rate is stored to six places (app.schemas.common)
_RATE_PLACES = TAX_RATE_PLACES


def _qbo_rate(source, lines, tax) -> Decimal:
    """QBO's sales tax as a document's rate (a fraction): its tax lines'
    percentages together, when they are all percentages of one taxable
    amount and that rate, as stored (_RATE_PLACES), applied to the lines
    marked taxable, gives QBO's tax to the cent, so an edit re-totals to
    QBO's total. 0 otherwise: the document keeps QBO's tax amount, and an
    edit keeps it as it is rather than working it out as 0.00."""
    from app.services.accounting import _q

    if not tax:
        return Decimal("0")
    detail = _safe(source, "TxnTaxDetail")
    percents, bases = [], set()
    for tax_line in (_safe(detail, "TaxLine") or []) if detail else []:
        line_detail = _safe(tax_line, "TaxLineDetail")
        percent = _field(line_detail, "TaxPercent") if line_detail else None
        if (
            not line_detail
            or not _field(line_detail, "PercentBased")
            or percent is None
        ):
            return Decimal("0")
        percents.append(Decimal(str(percent)))
        base = _field(line_detail, "NetAmountTaxable")
        if base is not None:
            bases.add(Decimal(str(base)))
    if not percents or len(bases) > 1:
        return Decimal("0")
    rate = (sum(percents) / 100).quantize(_RATE_PLACES)
    taxable = _q(
        sum(
            (
                _q(line["quantity"] * line["rate"])
                for line in lines
                if line["is_taxable"]
            ),
            Decimal("0"),
        )
    )
    return rate if _q(taxable * rate) == _q(tax) else Decimal("0")


def _tax_base(source) -> Decimal | None:
    """The taxable amount QBO worked its tax out on: the NetAmountTaxable
    its tax lines share. None when they name different amounts, or none."""
    detail = _safe(source, "TxnTaxDetail")
    bases = set()
    for tax_line in (_safe(detail, "TaxLine") or []) if detail else []:
        base = _field(_safe(tax_line, "TaxLineDetail"), "NetAmountTaxable")
        if base is not None:
            bases.add(Decimal(str(base)))
    return bases.pop() if len(bases) == 1 else None


def _sales_line(db, qbo_line, detail) -> dict:
    """A QBO sales line (SalesItemLineDetail), as the import stores it."""
    item_id = None
    item_ref = _safe(detail, "ItemRef")
    if item_ref:
        item_map = get_mapping_by_qbo_id(db, "item", _safe(item_ref, "value", ""))
        if item_map:
            item_id = item_map.slowbooks_id
    taxable = _tax_code(detail)
    return {
        "item_id": item_id,
        "description": _safe(qbo_line, "Description") or None,
        "quantity": _safe_decimal(detail, "Qty") or Decimal("1"),
        "rate": _safe_decimal(detail, "UnitPrice"),
        "amount": _safe_decimal(qbo_line, "Amount"),
        "is_taxable": True if taxable is None else taxable,
    }


def _discount_item(db, detail) -> int:
    """The item a QBO discount comes in on: one for each discount account,
    a service item whose income account is QBO's discount account, so a
    document of ours books its discount where QBO did. A negative line may
    stand on it (qbo_common.is_discount_item)."""
    from app.services.qbo_common import DISCOUNT_ITEM

    ref = _field(detail, "DiscountAccountRef")
    qbo_account = str(_safe(ref, "value", "") or "") if ref else ""
    found = (
        db.query(QBOMapping)
        .filter_by(entity_type=DISCOUNT_ITEM, qbo_id=qbo_account)
        .first()
    )
    if found is not None and db.get(Item, found.slowbooks_id) is not None:
        return found.slowbooks_id
    account = None
    if qbo_account:
        account_map = get_mapping_by_qbo_id(db, "account", qbo_account)
        account = db.get(Account, account_map.slowbooks_id) if account_map else None
        if account is None:
            named = _safe(ref, "name", "")
            raise DataProblem(
                f"its discount account, QBO #{qbo_account}"
                + (f" ({named})" if named else "")
                + ", has not been imported; import Accounts, then this again"
            )
    names = {name for (name,) in db.query(Item.name)}
    name = "Discount"
    if name in names and account is not None:
        name = f"Discount ({account.name})"
    base, n = name, 2
    while name in names:
        name, n = f"{base} {n}", n + 1
    item = Item(
        name=name,
        item_type=ItemType.SERVICE,
        description="Discounts on QuickBooks Online invoices and sales receipts",
        rate=Decimal("0"),
        income_account_id=account.id if account is not None else None,
        is_active=True,
    )
    db.add(item)
    db.flush()
    if found is not None:
        found.slowbooks_id = item.id
    else:
        create_mapping(db, DISCOUNT_ITEM, item.id, qbo_account)
    return item.id


def _discount_words(qbo_line, detail) -> str:
    """ "Discount 10%" (a percentage) or "Discount", unless QBO says."""
    said = _safe(qbo_line, "Description")
    if said:
        return said
    percent = _field(detail, "DiscountPercent")
    if _field(detail, "PercentBased") and percent:
        return f"Discount {Decimal(str(percent)).normalize():f}%"
    return "Discount"


def _discount_on_taxed(amount, after, taxed, sold, base) -> Decimal:
    """How much of a discount comes off the taxable amount. None of it when
    QBO works the tax out first (ApplyTaxAfterDiscount false). Otherwise
    what QBO took off it (its taxed lines less the amount it taxed, `base`),
    or all of it when every line is taxed, or the taxed lines' share."""
    from app.services.accounting import _q

    if not after or taxed <= 0:
        return Decimal("0")
    if base is not None and 0 <= taxed - base <= amount:
        return taxed - base
    if taxed >= sold:
        return amount
    return _q(amount * taxed / sold)


def _bundle_lines(db, qbo_line) -> list[dict]:
    """A QBO bundle (GroupLineDetail) as the lines of its items: each sales
    line in it as the import stores one (item, quantity, price, amount and
    taxable flag), so the stock and income of its items are where QBO has
    them. When the bundle's own amount differs from its items' (a price set
    on the bundle), the difference is a line named for the bundle, taxable
    when every item in it is. A bundle line reading 0.00 leaves its total
    to its items: the SDK reads an amount QBO leaves out as 0."""
    from app.services.accounting import _q

    group = _safe(qbo_line, "GroupLineDetail")
    lines = []
    for inner in (_safe(group, "Line") or []) if group else []:
        detail = _safe(inner, "SalesItemLineDetail")
        if _safe(inner, "DetailType", "") == "SalesItemLineDetail" and detail:
            lines.append(_sales_line(db, inner, detail))
    total = _safe_decimal(qbo_line, "Amount")
    difference = (
        _q(total - sum((ln["amount"] for ln in lines), Decimal("0")))
        if total
        else Decimal("0")
    )
    if difference:
        ref = _safe(group, "GroupItemRef") if group else None
        lines.append(
            {
                "item_id": None,
                "description": _safe(qbo_line, "Description")
                or (_safe(ref, "name", "") if ref else "")
                or "Bundle",
                "quantity": Decimal("1"),
                "rate": difference,
                "amount": difference,
                "is_taxable": bool(lines) and all(ln["is_taxable"] for ln in lines),
            }
        )
    return lines


def _document_lines(db, source) -> list[dict]:
    """The lines the import makes of a QBO invoice or sales receipt, in
    QBO's order: one for each sales line (SalesItemLineDetail), the lines of
    each bundle's items (GroupLineDetail, _bundle_lines), and its discount
    (DiscountLineDetail) as a negative line on the discount item for QBO's
    discount account (_discount_item). The lines add up to QBO's subtotal,
    an edit here re-totals to QBO's total, and a document of ours books the
    discount where QBO did. With the tax worked out after the discount
    (ApplyTaxAfterDiscount), the part of the discount QBO took off the
    taxable amount is a taxable line, the rest (the untaxed lines' share)
    an untaxed one, so the taxable amount is QBO's. Subtotal lines are
    QBO's arithmetic."""
    from app.services.accounting import _q

    ordered = []
    for qbo_line in _safe(source, "Line") or []:
        kind = _safe(qbo_line, "DetailType", "")
        if kind == "SalesItemLineDetail":
            detail = _safe(qbo_line, "SalesItemLineDetail")
            if detail:
                ordered.append(_sales_line(db, qbo_line, detail))
        elif kind == "GroupLineDetail":
            ordered.extend(_bundle_lines(db, qbo_line))
        elif kind == "DiscountLineDetail":
            ordered.append(qbo_line)
    sales = [line for line in ordered if isinstance(line, dict)]
    discounts = len(ordered) - len(sales)
    if not discounts:
        return sales
    taxed = sum((ln["amount"] for ln in sales if ln["is_taxable"]), Decimal("0"))
    sold = sum((ln["amount"] for ln in sales), Decimal("0"))
    after = bool(_field(source, "ApplyTaxAfterDiscount"))
    txn_tax = _safe(source, "TxnTaxDetail")
    tax = _safe_decimal(txn_tax, "TotalTax") if txn_tax else Decimal("0")
    base = _tax_base(source) if tax and discounts == 1 else None
    lines = []
    for entry in ordered:
        if isinstance(entry, dict):
            lines.append(entry)
            continue
        amount = _q(abs(_safe_decimal(entry, "Amount")))
        detail = _safe(entry, "DiscountLineDetail")
        if not amount or not detail:
            continue
        item_id = _discount_item(db, detail)
        words = _discount_words(entry, detail)
        on_taxed = _discount_on_taxed(amount, after, taxed, sold, base)
        parts = [(on_taxed, True), (amount - on_taxed, False)]
        parts = [(part, taxable) for part, taxable in parts if part]
        for part, taxable in parts:
            lines.append(
                {
                    "item_id": item_id,
                    "description": (
                        words
                        if len(parts) == 1
                        else f"{words} on {'taxable' if taxable else 'non-taxable'} lines"
                    ),
                    "quantity": Decimal("1"),
                    "rate": -part,
                    "amount": -part,
                    "is_taxable": taxable,
                }
            )
    return lines


def _lines_short(lines, subtotal) -> Decimal:
    """How far a document's lines fall short of QBO's subtotal: 0 when they
    add up to it, as they do when every line came across."""
    from app.services.accounting import _q

    return _q(
        subtotal
        - sum(
            (_q(ln["quantity"] * ln["rate"]) for ln in lines),
            Decimal("0"),
        )
    )


def _note_lines_short(lines, subtotal) -> None:
    """Say so in the import log when a document's lines don't add up to its
    total before tax: a line of a kind the import doesn't bring across
    makes the difference, which an edit here posts to income."""
    short = _lines_short(lines, subtotal)
    if short:
        qbo_progress.emit(
            "note",
            f"Its lines here come to {subtotal - short:.2f} and its total before "
            f"tax in QuickBooks Online to {subtotal:.2f}: a line of a kind the "
            f"import doesn't bring across makes up the {abs(short):.2f}. If it "
            "is edited here, that part of its total posts to your income "
            "account.",
            level="warning",
            code="IMPORT_LINES_SHORT",
        )


def _document_values(db, source, kind) -> dict:
    """What the document import makes of a QBO invoice or sales receipt: the
    same reading as when it first came in, for bringing one it made up to
    date when QBO changes it."""
    customer_id, job_id = _resolve_customer(db, _safe(source, "CustomerRef"))
    total = _safe_decimal(source, "TotalAmt")
    tax = Decimal("0")
    txn_tax = _safe(source, "TxnTaxDetail")
    if txn_tax:
        tax = _safe_decimal(txn_tax, "TotalTax")
    day = _parse_qbo_date(_safe(source, "TxnDate"))
    lines = _document_lines(db, source)
    due = day if kind == "sales_receipt" else _parse_qbo_date(_safe(source, "DueDate"))
    return {
        "customer_id": customer_id,
        "job_id": job_id,
        "date": day,
        "due_date": due,
        "subtotal": total - tax,
        "tax_amount": tax,
        "tax_rate": _qbo_rate(source, lines, tax),
        "total": total,
        "lines": lines,
    }


def _same_document(invoice, values) -> bool:
    header = ("customer_id", "job_id", "date", "due_date", "subtotal", "tax_amount")
    if any(getattr(invoice, key) != values[key] for key in header + ("total",)):
        return False
    if Decimal(str(invoice.tax_rate or 0)) != values["tax_rate"]:
        return False
    ours = [
        (ln.item_id, ln.description, ln.quantity, ln.rate, ln.amount, ln.is_taxable)
        for ln in sorted(invoice.lines, key=lambda ln: ln.line_order)
    ]
    theirs = [
        (
            ln["item_id"],
            ln["description"],
            ln["quantity"],
            ln["rate"],
            ln["amount"],
            ln["is_taxable"],
        )
        for ln in values["lines"]
    ]
    return ours == theirs


def _changed_in_qbo(mapping, source) -> str | None:
    """QBO's new SyncToken when QBO changed a document the import still owns;
    None when it did not (or the document changed here)."""
    token = str(_safe(source, "SyncToken", "") or "")
    if not token or mapping.qbo_sync_token in NOT_OWNED:
        return None
    return token if token != str(mapping.qbo_sync_token or "") else None


def _not_applied(errors, entity, source, noun, why) -> None:
    qbo_progress.append_error(
        errors,
        {
            "entity": entity,
            "qbo_id": str(_safe(source, "Id", "")),
            "document_number": _safe(source, "DocNumber")
            or _safe(source, "PaymentRefNum"),
            "code": "IMPORT_QBO_CHANGE_NOT_APPLIED",
            "message": (
                f"{_source_context(_LABEL[entity], source)} was changed in QuickBooks Online "
                f"after it was imported, and that was not applied here: {why}. The "
                f"{noun} here keeps what was imported."
            ),
        },
    )


_BROUGHT = {"DiscountLineDetail": "its discount", "GroupLineDetail": "its bundles"}


def _bring_lines_across(db, mapping, invoice, source, kind) -> bool:
    """A document imported before discounts and bundles came across
    (2.18.0), and not changed in QBO since: its lines fall short of its
    subtotal by what QBO's discount or bundle lines now bring. Its lines are
    brought up to date; its amounts are QBO's already and stay as they are.
    False, and nothing changed, when there is nothing to bring, when it is
    no longer the import's (voided or edited here), or when its period is
    closed."""
    from app.models.invoices import InvoiceStatus
    from app.services.inventory_hooks import (
        reconcile_invoice_inventory_delta,
        snapshot_invoice_lines,
    )
    from app.services.qbo_documents import closed_on, local_cost_live

    brought = [
        words
        for kind_name, words in _BROUGHT.items()
        if any(
            _safe(line, "DetailType", "") == kind_name
            for line in _safe(source, "Line") or []
        )
    ]
    if (
        invoice is None
        or mapping.qbo_sync_token in NOT_OWNED
        or invoice.transaction_id is not None
        or invoice.status == InvoiceStatus.VOID
        or not brought
    ):
        return False
    ours = [{"quantity": ln.quantity, "rate": ln.rate} for ln in invoice.lines]
    if not _lines_short(ours, invoice.subtotal):
        return False
    values = _document_values(db, source, kind)
    header = ("customer_id", "job_id", "date", "due_date", "subtotal", "tax_amount")
    if (
        any(getattr(invoice, key) != values[key] for key in header + ("total",))
        or _lines_short(values["lines"], values["subtotal"])
        or closed_on(db, invoice.date)
    ):
        return False
    old_lines = snapshot_invoice_lines(invoice)
    invoice.tax_rate = values["tax_rate"]
    audited_bulk_delete(
        db, db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id)
    )
    db.flush()
    for order, line in enumerate(values["lines"]):
        db.add(InvoiceLine(invoice_id=invoice.id, line_order=order, **line))
    db.flush()
    db.refresh(invoice)
    reconcile_invoice_inventory_delta(
        db,
        invoice,
        old_lines,
        txn_date=invoice.date,
        post_journal=local_cost_live(db, invoice),
    )
    noun = "sales receipt" if kind == "sales_receipt" else "invoice"
    qbo_progress.emit(
        "update",
        f"{_source_context(_LABEL[kind], source)}: {' and '.join(brought)} came "
        f"across; {noun} {invoice.invoice_number} has those lines now, and its "
        f"total ({invoice.total:,.2f}) is as it was",
        code="IMPORT_QBO_LINES_ADDED",
    )
    return True


def _refresh_invoice(db, mapping, source, kind, errors) -> None:
    """Bring an invoice or sales receipt the import made up to date with QBO,
    while it is still the import's own: not voided or edited here. Its
    posting follows in the ledger import, under the same conditions."""
    from app.models.invoices import InvoiceStatus
    from app.services.inventory_hooks import (
        reconcile_invoice_inventory_delta,
        snapshot_invoice_lines,
    )
    from app.services.qbo_documents import (
        closed_on,
        current_invoice_for_import,
        import_posting,
        local_cost_live,
        paid_past_new_total,
        why_not_changed,
    )

    invoice = current_invoice_for_import(db, mapping.slowbooks_id)
    token = _changed_in_qbo(mapping, source)
    if token is None and _bring_lines_across(db, mapping, invoice, source, kind):
        return
    if (
        token is None
        or invoice is None
        or invoice.transaction_id is not None
        or invoice.status == InvoiceStatus.VOID
    ):
        qbo_progress.skipped()
        return
    values = _document_values(db, source, kind)
    noun = "sales receipt" if kind == "sales_receipt" else "invoice"
    if _same_document(invoice, values):
        mapping.qbo_sync_token = token  # a change that is not the document's
        qbo_progress.skipped("Verified: its amounts and lines match QBO")
        return
    if values["total"] == 0 and invoice.total != 0:
        # How QBO shows a void; Posted Ledger Activity brings a void in.
        qbo_progress.skipped("Reads 0.00 in QuickBooks Online; kept as imported")
        return
    found = import_posting(db, mapping)
    why = (
        closed_on(db, invoice.date)
        or closed_on(db, values["date"])
        or (found and why_not_changed(db, found[1], values["date"]))
    )
    if why:
        _not_applied(errors, kind, source, noun, why)
        return
    # Paid here past QBO's new total: the ledger import leaves its posting
    # too, and says so in the same line (qbo_documents.paid_past_new_total).
    held = kind == "invoice" and paid_past_new_total(
        invoice, _safe(source, "Id", ""), values["total"]
    )
    if held:
        qbo_progress.append_error(errors, held)
        return
    old_lines = snapshot_invoice_lines(invoice)
    was = invoice.total
    for key in ("customer_id", "job_id", "date", "due_date", "subtotal"):
        setattr(invoice, key, values[key])
    invoice.tax_amount, invoice.total = values["tax_amount"], values["total"]
    invoice.tax_rate = values["tax_rate"]
    audited_bulk_delete(
        db, db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id)
    )
    db.flush()
    for order, line in enumerate(values["lines"]):
        db.add(InvoiceLine(invoice_id=invoice.id, line_order=order, **line))
    db.flush()
    db.refresh(invoice)
    _note_lines_short(values["lines"], values["subtotal"])
    if kind == "sales_receipt":
        for alloc in invoice.payment_allocations:
            payment = alloc.payment
            if payment and payment.transaction_id is None and not payment.is_voided:
                payment.amount, payment.date = values["total"], values["date"]
                alloc.amount = values["total"]
        invoice.amount_paid = values["total"]
    invoice.balance_due = invoice.total - (invoice.amount_paid or 0)
    invoice.status = (
        InvoiceStatus.PAID
        if invoice.balance_due <= 0 and invoice.total > 0
        else (
            InvoiceStatus.PARTIAL
            if (invoice.amount_paid or 0) > 0
            else InvoiceStatus.SENT
        )
    )
    reconcile_invoice_inventory_delta(
        db,
        invoice,
        old_lines,
        txn_date=invoice.date,
        post_journal=local_cost_live(db, invoice),
    )
    mapping.qbo_sync_token = token
    qbo_progress.emit(
        "update",
        f"{_source_context(_LABEL[kind], source)} changed in QuickBooks Online: {noun} "
        f"{invoice.invoice_number} brought up to date here (total {was:,.2f} "
        f"-> {invoice.total:,.2f})",
        code="IMPORT_QBO_CHANGE_APPLIED",
    )


def _refresh_payment(db, mapping, source, errors) -> None:
    """Bring a payment the import made up to date with QBO (amount, date,
    the invoices it pays), while it is still the import's own."""
    from app.models.invoices import InvoiceStatus
    from app.services.qbo_documents import (
        closed_on,
        import_posting,
        pay_invoice,
        unpay_invoice,
        why_not_changed,
    )

    payment = db.get(Payment, mapping.slowbooks_id)
    token = _changed_in_qbo(mapping, source)
    if (
        token is None
        or payment is None
        or payment.is_voided
        or payment.transaction_id is not None
    ):
        qbo_progress.skipped()
        return
    amount = _safe_decimal(source, "TotalAmt")
    day = _parse_qbo_date(_safe(source, "TxnDate"))
    applied = []
    for pmt_line in _safe(source, "Line") or []:
        line_amount = _safe_decimal(pmt_line, "Amount")
        for linked in _safe(pmt_line, "LinkedTxn") or []:
            if _safe(linked, "TxnType", "") != "Invoice":
                continue
            inv_map = get_mapping_by_qbo_id(db, "invoice", _safe(linked, "TxnId", ""))
            invoice = db.get(Invoice, inv_map.slowbooks_id) if inv_map else None
            if invoice is not None:
                applied.append((invoice, line_amount or amount))
    ours = sorted((a.invoice_id, a.amount) for a in payment.allocations)
    if (payment.amount, payment.date) == (amount, day) and ours == sorted(
        (invoice.id, value) for invoice, value in applied
    ):
        mapping.qbo_sync_token = token
        qbo_progress.skipped("Verified: its amount and invoices match QBO")
        return
    if amount == 0 and payment.amount != 0:
        qbo_progress.skipped("Reads 0.00 in QuickBooks Online; kept as imported")
        return
    found = import_posting(db, mapping)
    why = closed_on(db, payment.date) or closed_on(db, day)
    why = why or (found and why_not_changed(db, found[1], day))
    for invoice, value in applied:
        already = sum(
            (a.amount for a in payment.allocations if a.invoice_id == invoice.id),
            Decimal("0"),
        )
        if invoice.status == InvoiceStatus.VOID:
            why = why or f"invoice {invoice.invoice_number} is void here"
        elif (invoice.amount_paid or 0) - already + value > invoice.total:
            why = (
                why
                or f"it would pay invoice {invoice.invoice_number} more than it owes"
            )
    if why:
        _not_applied(errors, "payment", source, "payment", why)
        return
    was = payment.amount
    for alloc in list(payment.allocations):
        unpay_invoice(alloc.invoice, alloc.amount)
        db.delete(alloc)
    db.flush()
    payment.amount, payment.date = amount, day
    for invoice, value in applied:
        db.add(
            PaymentAllocation(
                payment_id=payment.id, invoice_id=invoice.id, amount=value
            )
        )
        pay_invoice(invoice, value)
    mapping.qbo_sync_token = token
    qbo_progress.emit(
        "update",
        f"{_source_context('Payment', source)} changed in QuickBooks Online: "
        f"payment #{payment.id} brought up to date here (amount {was:,.2f} -> "
        f"{amount:,.2f})",
        code="IMPORT_QBO_CHANGE_APPLIED",
    )


# ============================================================================

# Import functions
# ============================================================================


@qbo_progress.stage("accounts")
def import_accounts(db: Session) -> dict:
    """Import accounts from QBO into Slowbooks."""
    from quickbooks.objects.account import Account as QBOAccount

    client = get_qbo_client(db)
    imported = 0
    errors = []

    try:
        # Historical report postings can reference inactive/deleted accounts.
        qbo_accounts = list(
            {
                str(a.Id): a
                for a in (
                    _all_qbo_objects(QBOAccount, client)
                    + _all_inactive_qbo_accounts(QBOAccount, client)
                )
            }.values()
        )
        # Sort by FullyQualifiedName depth so parents come first
        qbo_accounts.sort(
            key=lambda a: (_safe(a, "FullyQualifiedName", "") or "").count(":")
        )
    except Exception as e:
        qbo_progress.append_error(
            errors,
            {
                "entity": "accounts",
                "message": f"Failed to query QBO: {qbo_progress.error_message(e, "QBO import")}",
            },
        )
        return {"imported": 0, "errors": errors}

    for qbo_acct in qbo_accounts:
        try:
            qbo_id = _safe(qbo_acct, "Id", "")
            qbo_progress.item(
                qbo_id,
                _safe(qbo_acct, "DocNumber")
                or _safe(qbo_acct, "DisplayName")
                or _safe(qbo_acct, "Name"),
            )
            if not qbo_id:
                continue

            qbo_type = _safe(qbo_acct, "AccountType", "Expense")

            active = getattr(qbo_acct, "Active", True) is not False

            # Earlier imports mapped bank/card accounts without their Banking
            # identity. Repair those mappings on the next import.
            mapping = get_mapping_by_qbo_id(db, "account", qbo_id)
            if mapping:
                existing = db.get(Account, mapping.slowbooks_id)
                if existing is not None:
                    _sync_active(db, existing, active, qbo_id)
                    if active:
                        _sync_bank_identity(db, existing, qbo_type)
                continue

            name = _safe(qbo_acct, "Name", "")
            if not name:
                continue

            # Check if name already exists in Slowbooks
            existing = db.query(Account).filter(Account.name == name).first()
            if existing:
                _sync_active(db, existing, active, qbo_id)
                if active:
                    _sync_bank_identity(db, existing, qbo_type)
                create_mapping(
                    db, "account", existing.id, qbo_id, _safe(qbo_acct, "SyncToken")
                )
                db.flush()
                continue

            # Map QBO account type to Slowbooks
            acct_type = QBO_TO_ACCOUNT_TYPE.get(qbo_type, AccountType.EXPENSE)

            # Resolve parent account
            parent_id = None
            parent_ref = _safe(qbo_acct, "ParentRef")
            if parent_ref:
                parent_qbo_id = _safe(parent_ref, "value", "")
                parent_map = get_mapping_by_qbo_id(db, "account", parent_qbo_id)
                if parent_map:
                    parent_id = parent_map.slowbooks_id

            acct = Account(
                name=name,
                account_type=acct_type,
                account_number=_safe(qbo_acct, "AcctNum") or None,
                description=_safe(qbo_acct, "Description") or None,
                parent_id=parent_id,
                is_active=active,
                balance=_safe_decimal(qbo_acct, "CurrentBalance"),
            )
            db.add(acct)
            db.flush()
            if active:
                _sync_bank_identity(db, acct, qbo_type)

            create_mapping(db, "account", acct.id, qbo_id, _safe(qbo_acct, "SyncToken"))
            imported += 1
            qbo_progress.created()

        except Exception as e:
            qbo_progress.append_error(
                errors,
                {
                    "entity": "account",
                    "qbo_id": str(qbo_id),
                    "message": qbo_progress.error_message(e, "QBO import"),
                },
            )

    return {"imported": imported, "errors": errors}


@qbo_progress.stage("customers")
def import_customers(db: Session) -> dict:
    """Import customers from QBO into Slowbooks."""
    from quickbooks.objects.customer import Customer as QBOCustomer

    client = get_qbo_client(db)
    imported = 0
    errors = []

    try:
        qbo_customers = _all_qbo_objects(QBOCustomer, client)
    except Exception as e:
        qbo_progress.append_error(
            errors,
            {
                "entity": "customers",
                "message": f"Failed to query QBO: {qbo_progress.error_message(e, "QBO import")}",
            },
        )
        return {"imported": 0, "errors": errors}

    # Sub-customers (QBO "Job": true, the Online "Projects" flavour) become
    # Jobs under their parent, so parents must land first.
    qbo_customers = sorted(qbo_customers, key=lambda c: bool(_safe(c, "Job", False)))

    for qbo_cust in qbo_customers:
        try:
            qbo_id = _safe(qbo_cust, "Id", "")
            qbo_progress.item(
                qbo_id,
                _safe(qbo_cust, "DocNumber")
                or _safe(qbo_cust, "DisplayName")
                or _safe(qbo_cust, "Name"),
            )
            if not qbo_id:
                continue

            display_name = _safe(qbo_cust, "DisplayName", "")
            if not display_name:
                continue

            parent_ref = _safe(qbo_cust, "ParentRef")
            if _safe(qbo_cust, "Job", False) and parent_ref:
                if get_mapping_by_qbo_id(db, "job", qbo_id):
                    continue
                parent_qbo_id = (
                    _safe(parent_ref, "value", "")
                    if not isinstance(parent_ref, str)
                    else parent_ref
                )
                parent_map = get_mapping_by_qbo_id(db, "customer", str(parent_qbo_id))
                parent = (
                    db.get(Customer, parent_map.slowbooks_id) if parent_map else None
                )
                if parent is None:
                    # Fall back to the qualified name ("Parent:Child")
                    from app.services.jobs_service import resolve_customer_and_job

                    fq = _safe(qbo_cust, "FullyQualifiedName", "") or display_name
                    parent, job = resolve_customer_and_job(db, fq)
                else:
                    from app.services.jobs_service import get_or_create_job

                    job = get_or_create_job(db, parent.id, display_name)
                if job is not None:
                    create_mapping(
                        db, "job", job.id, qbo_id, _safe(qbo_cust, "SyncToken")
                    )
                    db.flush()
                    imported += 1
                    qbo_progress.created(qbo_id)
                continue

            if get_mapping_by_qbo_id(db, "customer", qbo_id):
                continue

            # Check by name
            existing = db.query(Customer).filter(Customer.name == display_name).first()
            if existing:
                create_mapping(
                    db, "customer", existing.id, qbo_id, _safe(qbo_cust, "SyncToken")
                )
                db.flush()
                continue

            # Extract billing address
            bill_addr = _safe(qbo_cust, "BillAddr")
            bill_address1 = _safe(bill_addr, "Line1") if bill_addr else None
            bill_address2 = _safe(bill_addr, "Line2") if bill_addr else None
            bill_city = _safe(bill_addr, "City") if bill_addr else None
            bill_state = (
                _safe(bill_addr, "CountrySubDivisionCode") if bill_addr else None
            )
            bill_zip = _safe(bill_addr, "PostalCode") if bill_addr else None

            # Extract shipping address
            ship_addr = _safe(qbo_cust, "ShipAddr")
            ship_address1 = _safe(ship_addr, "Line1") if ship_addr else None
            ship_address2 = _safe(ship_addr, "Line2") if ship_addr else None
            ship_city = _safe(ship_addr, "City") if ship_addr else None
            ship_state = (
                _safe(ship_addr, "CountrySubDivisionCode") if ship_addr else None
            )
            ship_zip = _safe(ship_addr, "PostalCode") if ship_addr else None

            # Extract primary email/phone
            email_addr = _safe(qbo_cust, "PrimaryEmailAddr")
            email = _safe(email_addr, "Address") if email_addr else None

            phone_obj = _safe(qbo_cust, "PrimaryPhone")
            phone = _safe(phone_obj, "FreeFormNumber") if phone_obj else None

            mobile_obj = _safe(qbo_cust, "Mobile")
            mobile = _safe(mobile_obj, "FreeFormNumber") if mobile_obj else None

            fax_obj = _safe(qbo_cust, "Fax")
            fax = _safe(fax_obj, "FreeFormNumber") if fax_obj else None

            # Resolve payment terms
            terms = "Net 30"
            terms_ref = _safe(qbo_cust, "SalesTermRef")
            if terms_ref:
                terms = _safe(terms_ref, "name", "Net 30") or "Net 30"

            cust = Customer(
                name=display_name,
                company=_safe(qbo_cust, "CompanyName") or None,
                email=email,
                phone=phone,
                mobile=mobile,
                fax=fax,
                website=(
                    _safe(qbo_cust, "WebAddr", {}).get("URI")
                    if isinstance(_safe(qbo_cust, "WebAddr"), dict)
                    else None
                ),
                bill_address1=bill_address1,
                bill_address2=bill_address2,
                bill_city=bill_city,
                bill_state=bill_state,
                bill_zip=bill_zip,
                ship_address1=ship_address1,
                ship_address2=ship_address2,
                ship_city=ship_city,
                ship_state=ship_state,
                ship_zip=ship_zip,
                terms=terms,
                tax_id=_safe(qbo_cust, "PrimaryTaxIdentifier") or None,
                is_taxable=_safe(qbo_cust, "Taxable", True),
                is_active=_safe(qbo_cust, "Active", True),
                balance=_safe_decimal(qbo_cust, "Balance"),
                notes=_safe(qbo_cust, "Notes") or None,
            )
            db.add(cust)
            db.flush()

            create_mapping(
                db, "customer", cust.id, qbo_id, _safe(qbo_cust, "SyncToken")
            )
            imported += 1
            qbo_progress.created()

        except Exception as e:
            qbo_progress.append_error(
                errors,
                {
                    "entity": "customer",
                    "qbo_id": str(qbo_id),
                    "message": qbo_progress.error_message(e, "QBO import"),
                },
            )

    return {"imported": imported, "errors": errors}


@qbo_progress.stage("vendors")
def import_vendors(db: Session) -> dict:
    """Import vendors from QBO into Slowbooks."""
    from quickbooks.objects.vendor import Vendor as QBOVendor

    client = get_qbo_client(db)
    imported = 0
    errors = []

    try:
        qbo_vendors = _all_qbo_objects(QBOVendor, client)
    except Exception as e:
        qbo_progress.append_error(
            errors,
            {
                "entity": "vendors",
                "message": f"Failed to query QBO: {qbo_progress.error_message(e, "QBO import")}",
            },
        )
        return {"imported": 0, "errors": errors}

    for qbo_vend in qbo_vendors:
        try:
            qbo_id = _safe(qbo_vend, "Id", "")
            qbo_progress.item(
                qbo_id,
                _safe(qbo_vend, "DocNumber")
                or _safe(qbo_vend, "DisplayName")
                or _safe(qbo_vend, "Name"),
            )
            if not qbo_id:
                continue

            if get_mapping_by_qbo_id(db, "vendor", qbo_id):
                continue

            display_name = _safe(qbo_vend, "DisplayName", "")
            if not display_name:
                continue

            existing = db.query(Vendor).filter(Vendor.name == display_name).first()
            if existing:
                create_mapping(
                    db, "vendor", existing.id, qbo_id, _safe(qbo_vend, "SyncToken")
                )
                db.flush()
                continue

            # Extract address
            addr = _safe(qbo_vend, "BillAddr")
            address1 = _safe(addr, "Line1") if addr else None
            address2 = _safe(addr, "Line2") if addr else None
            city = _safe(addr, "City") if addr else None
            state = _safe(addr, "CountrySubDivisionCode") if addr else None
            zipcode = _safe(addr, "PostalCode") if addr else None

            email_addr = _safe(qbo_vend, "PrimaryEmailAddr")
            email = _safe(email_addr, "Address") if email_addr else None

            phone_obj = _safe(qbo_vend, "PrimaryPhone")
            phone = _safe(phone_obj, "FreeFormNumber") if phone_obj else None

            fax_obj = _safe(qbo_vend, "Fax")
            fax = _safe(fax_obj, "FreeFormNumber") if fax_obj else None

            terms = "Net 30"
            terms_ref = _safe(qbo_vend, "TermRef")
            if terms_ref:
                terms = _safe(terms_ref, "name", "Net 30") or "Net 30"

            vend = Vendor(
                name=display_name,
                company=_safe(qbo_vend, "CompanyName") or None,
                email=email,
                phone=phone,
                fax=fax,
                address1=address1,
                address2=address2,
                city=city,
                state=state,
                zip=zipcode,
                terms=terms,
                tax_id=_safe(qbo_vend, "TaxIdentifier") or None,
                account_number=_safe(qbo_vend, "AcctNum") or None,
                is_active=_safe(qbo_vend, "Active", True),
                balance=_safe_decimal(qbo_vend, "Balance"),
                notes=_safe(qbo_vend, "Notes") or None,
            )
            db.add(vend)
            db.flush()

            create_mapping(db, "vendor", vend.id, qbo_id, _safe(qbo_vend, "SyncToken"))
            imported += 1
            qbo_progress.created()

        except Exception as e:
            qbo_progress.append_error(
                errors,
                {
                    "entity": "vendor",
                    "qbo_id": str(qbo_id),
                    "message": qbo_progress.error_message(e, "QBO import"),
                },
            )

    return {"imported": imported, "errors": errors}


@qbo_progress.stage("items")
def import_items(db: Session) -> dict:
    """Import items from QBO into Slowbooks."""
    from quickbooks.objects.item import Item as QBOItem

    client = get_qbo_client(db)
    imported = 0
    errors = []

    try:
        qbo_items = _all_qbo_objects(QBOItem, client)
    except Exception as e:
        qbo_progress.append_error(
            errors,
            {
                "entity": "items",
                "message": f"Failed to query QBO: {qbo_progress.error_message(e, "QBO import")}",
            },
        )
        return {"imported": 0, "errors": errors}

    for qbo_item in qbo_items:
        try:
            qbo_id = _safe(qbo_item, "Id", "")
            qbo_progress.item(
                qbo_id,
                _safe(qbo_item, "DocNumber")
                or _safe(qbo_item, "DisplayName")
                or _safe(qbo_item, "Name"),
            )
            if not qbo_id:
                continue

            if get_mapping_by_qbo_id(db, "item", qbo_id):
                continue

            name = _safe(qbo_item, "Name", "")
            if not name:
                continue

            existing = db.query(Item).filter(Item.name == name).first()
            if existing:
                create_mapping(
                    db, "item", existing.id, qbo_id, _safe(qbo_item, "SyncToken")
                )
                db.flush()
                continue

            qbo_type = _safe(qbo_item, "Type", "Service")
            item_type = QBO_TO_ITEM_TYPE.get(qbo_type, ItemType.SERVICE)

            # Resolve income account
            income_account_id = None
            income_ref = _safe(qbo_item, "IncomeAccountRef")
            if income_ref:
                income_qbo_id = _safe(income_ref, "value", "")
                income_map = get_mapping_by_qbo_id(db, "account", income_qbo_id)
                if income_map:
                    income_account_id = income_map.slowbooks_id

            # Resolve expense account
            expense_account_id = None
            expense_ref = _safe(qbo_item, "ExpenseAccountRef")
            if expense_ref:
                expense_qbo_id = _safe(expense_ref, "value", "")
                expense_map = get_mapping_by_qbo_id(db, "account", expense_qbo_id)
                if expense_map:
                    expense_account_id = expense_map.slowbooks_id

            item = Item(
                name=name,
                item_type=item_type,
                description=_safe(qbo_item, "Description") or None,
                rate=_safe_decimal(qbo_item, "UnitPrice"),
                cost=_safe_decimal(qbo_item, "PurchaseCost"),
                income_account_id=income_account_id,
                expense_account_id=expense_account_id,
                is_taxable=_safe(qbo_item, "Taxable", False),
                is_active=_safe(qbo_item, "Active", True),
            )
            db.add(item)
            db.flush()

            create_mapping(db, "item", item.id, qbo_id, _safe(qbo_item, "SyncToken"))
            imported += 1
            qbo_progress.created()

        except Exception as e:
            qbo_progress.append_error(
                errors,
                {
                    "entity": "item",
                    "qbo_id": str(qbo_id),
                    "message": qbo_progress.error_message(e, "QBO import"),
                },
            )

    return {"imported": imported, "errors": errors}


@qbo_progress.stage("invoices")
def import_invoices(db: Session) -> dict:
    """Import invoices from QBO into Slowbooks."""
    from quickbooks.objects.invoice import Invoice as QBOInvoice

    client = get_qbo_client(db)
    imported = 0
    errors = []

    try:
        qbo_invoices = _all_qbo_objects(QBOInvoice, client)
    except Exception as e:
        qbo_progress.append_error(
            errors,
            {
                "entity": "invoices",
                "message": f"Failed to query QBO: {qbo_progress.error_message(e, "QBO import")}",
            },
        )
        return {"imported": 0, "errors": errors}

    for qbo_inv in qbo_invoices:
        try:
            qbo_id = _safe(qbo_inv, "Id", "")
            qbo_progress.item(
                qbo_id,
                _safe(qbo_inv, "DocNumber")
                or _safe(qbo_inv, "DisplayName")
                or _safe(qbo_inv, "Name"),
            )
            if not qbo_id:
                continue

            mapping = (
                db.query(QBOMapping)
                .filter_by(entity_type="invoice", qbo_id=str(qbo_id))
                .first()
            )
            if mapping:
                _refresh_invoice(db, mapping, qbo_inv, "invoice", errors)
                continue

            doc_num = _safe(qbo_inv, "DocNumber", "")

            # Check by invoice number for dedup
            if doc_num:
                existing = (
                    db.query(Invoice).filter(Invoice.invoice_number == doc_num).first()
                )
                if existing:
                    create_mapping(
                        db, "invoice", existing.id, qbo_id, _safe(qbo_inv, "SyncToken")
                    )
                    db.flush()
                    continue

            customer_id, job_id = _resolve_customer(db, _safe(qbo_inv, "CustomerRef"))

            # Determine status from balance
            total_amt = _safe_decimal(qbo_inv, "TotalAmt")
            balance = _safe_decimal(qbo_inv, "Balance")
            if balance == total_amt:
                status = InvoiceStatus.SENT
            elif balance > 0 and balance < total_amt:
                status = InvoiceStatus.PARTIAL
            elif balance == 0 and total_amt > 0:
                status = InvoiceStatus.PAID
            else:
                status = InvoiceStatus.SENT

            inv_date = _parse_qbo_date(_safe(qbo_inv, "TxnDate"))
            due_date = _parse_qbo_date(_safe(qbo_inv, "DueDate"))

            # Extract tax
            tax_amount = Decimal("0")
            txn_tax = _safe(qbo_inv, "TxnTaxDetail")
            if txn_tax:
                tax_amount = _safe_decimal(txn_tax, "TotalTax")

            subtotal = total_amt - tax_amount
            amount_paid = total_amt - balance

            invoice = Invoice(
                invoice_number=doc_num or None,
                customer_id=customer_id,
                job_id=job_id,
                date=inv_date,
                due_date=due_date,
                terms=(
                    _safe(qbo_inv, "SalesTermRef", {}).get("name", "Net 30")
                    if isinstance(_safe(qbo_inv, "SalesTermRef"), dict)
                    else "Net 30"
                ),
                status=status,
                subtotal=subtotal,
                tax_rate=Decimal("0"),
                tax_amount=tax_amount,
                total=total_amt,
                amount_paid=amount_paid,
                balance_due=balance,
                notes=(
                    _safe(qbo_inv, "CustomerMemo", {}).get("value")
                    if isinstance(_safe(qbo_inv, "CustomerMemo"), dict)
                    else None
                ),
            )
            db.add(invoice)
            db.flush()

            # Its sales lines and its discount (_document_lines), and QBO's
            # tax as a rate when one rate gives it (_qbo_rate).
            made = _document_lines(db, qbo_inv)
            for order, line in enumerate(made):
                db.add(InvoiceLine(invoice_id=invoice.id, line_order=order, **line))
            invoice.tax_rate = _qbo_rate(qbo_inv, made, tax_amount)
            _note_lines_short(made, subtotal)

            create_mapping(
                db, "invoice", invoice.id, qbo_id, _safe(qbo_inv, "SyncToken")
            )

            # Phase 11 (audit fix): QBO-imported invoices must also move
            # inventory for tracked items. QBO itself manages inventory so
            # we only touch items that are track_inventory=True on OUR side.
            # The goods are costed once: here, at the local average cost,
            # unless the QBO ledger import has already posted this sale,
            # QBO's own cost of goods included (it takes back a local cost
            # posted before it: qbo_ledger_import._replace_import_cogs).
            db.flush()
            db.refresh(invoice)
            from app.services.inventory_hooks import post_sale_for_invoice

            post_sale_for_invoice(
                db,
                invoice,
                txn_date=invoice.date,
                post_journal=ledger_posting(db, "Invoice", qbo_id) is None,
            )

            imported += 1
            qbo_progress.created()

        except Exception as e:
            qbo_progress.append_error(
                errors,
                {
                    "entity": "invoice",
                    "qbo_id": str(qbo_id),
                    "code": getattr(e, "error_code", None)
                    or (
                        "IMPORT_VALIDATION"
                        if isinstance(e, DataProblem)
                        else "IMPORT_OPERATION_FAILED"
                    ),
                    "document_number": _safe(qbo_inv, "DocNumber")
                    or _safe(qbo_inv, "PaymentRefNum"),
                    "message": _source_context("invoice", qbo_inv)
                    + ": "
                    + qbo_progress.error_message(e, "QBO import"),
                },
            )

    return {"imported": imported, "errors": errors}


@qbo_progress.stage("payments")
def import_payments(db: Session) -> dict:
    """Import payments from QBO into Slowbooks."""
    from quickbooks.objects.payment import Payment as QBOPayment

    client = get_qbo_client(db)
    imported = 0
    errors = []

    try:
        qbo_payments = _all_qbo_objects(QBOPayment, client)
    except Exception as e:
        qbo_progress.append_error(
            errors,
            {
                "entity": "payments",
                "message": f"Failed to query QBO: {qbo_progress.error_message(e, "QBO import")}",
            },
        )
        return {"imported": 0, "errors": errors}

    for qbo_pmt in qbo_payments:
        try:
            qbo_id = _safe(qbo_pmt, "Id", "")
            qbo_progress.item(
                qbo_id,
                _safe(qbo_pmt, "DocNumber")
                or _safe(qbo_pmt, "PaymentRefNum")
                or _safe(qbo_pmt, "DisplayName")
                or _safe(qbo_pmt, "Name"),
            )
            if not qbo_id:
                continue

            mapping = (
                db.query(QBOMapping)
                .filter_by(entity_type="payment", qbo_id=str(qbo_id))
                .first()
            )
            if mapping:
                _refresh_payment(db, mapping, qbo_pmt, errors)
                continue

            customer_id, _ = _resolve_customer(db, _safe(qbo_pmt, "CustomerRef"))

            amount = _safe_decimal(qbo_pmt, "TotalAmt")
            pmt_date = _parse_qbo_date(_safe(qbo_pmt, "TxnDate"))

            # Resolve deposit account
            deposit_account_id = None
            deposit_ref = _safe(qbo_pmt, "DepositToAccountRef")
            if deposit_ref:
                deposit_qbo_id = _safe(deposit_ref, "value", "")
                deposit_map = get_mapping_by_qbo_id(db, "account", deposit_qbo_id)
                if deposit_map:
                    deposit_account_id = deposit_map.slowbooks_id

            payment = Payment(
                customer_id=customer_id,
                date=pmt_date,
                amount=amount,
                method=(
                    _safe(qbo_pmt, "PaymentMethodRef", {}).get("name")
                    if isinstance(_safe(qbo_pmt, "PaymentMethodRef"), dict)
                    else None
                ),
                reference=_safe(qbo_pmt, "PaymentRefNum") or None,
                deposit_to_account_id=deposit_account_id,
            )
            db.add(payment)
            db.flush()

            # Create allocations from QBO Line items
            lines = _safe(qbo_pmt, "Line") or []
            for pmt_line in lines:
                linked_txns = _safe(pmt_line, "LinkedTxn") or []
                line_amount = _safe_decimal(pmt_line, "Amount")
                for linked in linked_txns:
                    txn_type = _safe(linked, "TxnType", "")
                    txn_id = _safe(linked, "TxnId", "")
                    if txn_type == "Invoice" and txn_id:
                        inv_map = get_mapping_by_qbo_id(db, "invoice", txn_id)
                        if inv_map:
                            inv = (
                                db.query(Invoice)
                                .filter(Invoice.id == inv_map.slowbooks_id)
                                .first()
                            )
                            if inv:
                                alloc = PaymentAllocation(
                                    payment_id=payment.id,
                                    invoice_id=inv.id,
                                    amount=line_amount or amount,
                                )
                                db.add(alloc)

                                # Update invoice status
                                inv.amount_paid = (inv.amount_paid or Decimal("0")) + (
                                    line_amount or amount
                                )
                                inv.balance_due = inv.total - inv.amount_paid
                                if inv.balance_due <= 0:
                                    inv.status = InvoiceStatus.PAID
                                elif inv.amount_paid > 0:
                                    inv.status = InvoiceStatus.PARTIAL

            create_mapping(
                db, "payment", payment.id, qbo_id, _safe(qbo_pmt, "SyncToken")
            )
            imported += 1
            qbo_progress.created()

        except Exception as e:
            qbo_progress.append_error(
                errors,
                {
                    "entity": "payment",
                    "qbo_id": str(qbo_id),
                    "code": getattr(e, "error_code", None)
                    or (
                        "IMPORT_VALIDATION"
                        if isinstance(e, DataProblem)
                        else "IMPORT_OPERATION_FAILED"
                    ),
                    "document_number": _safe(qbo_pmt, "DocNumber")
                    or _safe(qbo_pmt, "PaymentRefNum"),
                    "message": _source_context("payment", qbo_pmt)
                    + ": "
                    + qbo_progress.error_message(e, "QBO import"),
                },
            )

    return {"imported": imported, "errors": errors}


@qbo_progress.stage("sales_receipts")
def import_sales_receipts(db: Session) -> dict:
    """Import sales receipts from QBO into Slowbooks.

    QBO's SalesReceipt is an invoice paid at the time of sale. Each one
    becomes an Invoice flagged is_sales_receipt (status PAID) plus a
    Payment for the full total — the same document pair the Enter Sales
    Receipts screen produces.
    """
    from quickbooks.objects.salesreceipt import SalesReceipt as QBOSalesReceipt

    client = get_qbo_client(db)
    imported = 0
    errors = []

    try:
        qbo_receipts = _all_qbo_objects(QBOSalesReceipt, client)
    except Exception as e:
        qbo_progress.append_error(
            errors,
            {
                "entity": "sales_receipts",
                "message": f"Failed to query QBO: {qbo_progress.error_message(e, "QBO import")}",
            },
        )
        return {"imported": 0, "errors": errors}

    for qbo_sr in qbo_receipts:
        try:
            qbo_id = _safe(qbo_sr, "Id", "")
            qbo_progress.item(
                qbo_id,
                _safe(qbo_sr, "DocNumber")
                or _safe(qbo_sr, "DisplayName")
                or _safe(qbo_sr, "Name"),
            )
            if not qbo_id:
                continue

            mapping = (
                db.query(QBOMapping)
                .filter_by(entity_type="sales_receipt", qbo_id=str(qbo_id))
                .first()
            )
            if mapping:
                _refresh_invoice(db, mapping, qbo_sr, "sales_receipt", errors)
                continue

            doc_num = _safe(qbo_sr, "DocNumber", "")

            # Dedup by document number against existing invoices/receipts
            if doc_num:
                existing = (
                    db.query(Invoice).filter(Invoice.invoice_number == doc_num).first()
                )
                if existing:
                    create_mapping(
                        db,
                        "sales_receipt",
                        existing.id,
                        qbo_id,
                        _safe(qbo_sr, "SyncToken"),
                    )
                    db.flush()
                    continue

            customer_id, job_id = _resolve_customer(db, _safe(qbo_sr, "CustomerRef"))

            total_amt = _safe_decimal(qbo_sr, "TotalAmt")
            sr_date = _parse_qbo_date(_safe(qbo_sr, "TxnDate"))

            # Extract tax
            tax_amount = Decimal("0")
            txn_tax = _safe(qbo_sr, "TxnTaxDetail")
            if txn_tax:
                tax_amount = _safe_decimal(txn_tax, "TotalTax")

            if not doc_num:
                from app.services.numbering import next_invoice_number

                doc_num = next_invoice_number(db)

            invoice = Invoice(
                invoice_number=doc_num,
                customer_id=customer_id,
                job_id=job_id,
                date=sr_date,
                due_date=sr_date,
                terms="Due on Receipt",
                status=InvoiceStatus.PAID,
                is_sales_receipt=True,
                subtotal=total_amt - tax_amount,
                tax_rate=Decimal("0"),
                tax_amount=tax_amount,
                total=total_amt,
                amount_paid=total_amt,
                balance_due=Decimal("0"),
                notes=(
                    _safe(qbo_sr, "CustomerMemo", {}).get("value")
                    if isinstance(_safe(qbo_sr, "CustomerMemo"), dict)
                    else None
                ),
            )
            db.add(invoice)
            db.flush()

            # Its sales lines and its discount (_document_lines), and QBO's
            # tax as a rate when one rate gives it (_qbo_rate).
            made = _document_lines(db, qbo_sr)
            for order, line in enumerate(made):
                db.add(InvoiceLine(invoice_id=invoice.id, line_order=order, **line))
            invoice.tax_rate = _qbo_rate(qbo_sr, made, tax_amount)
            _note_lines_short(made, total_amt - tax_amount)

            # Payment for the full total, deposited where QBO says
            deposit_account_id = None
            deposit_ref = _safe(qbo_sr, "DepositToAccountRef")
            if deposit_ref:
                deposit_qbo_id = _safe(deposit_ref, "value", "")
                deposit_map = get_mapping_by_qbo_id(db, "account", deposit_qbo_id)
                if deposit_map:
                    deposit_account_id = deposit_map.slowbooks_id

            payment = Payment(
                customer_id=customer_id,
                date=sr_date,
                amount=total_amt,
                method=(
                    _safe(qbo_sr, "PaymentMethodRef", {}).get("name")
                    if isinstance(_safe(qbo_sr, "PaymentMethodRef"), dict)
                    else None
                ),
                reference=_safe(qbo_sr, "PaymentRefNum") or None,
                deposit_to_account_id=deposit_account_id,
            )
            db.add(payment)
            db.flush()
            db.add(
                PaymentAllocation(
                    payment_id=payment.id, invoice_id=invoice.id, amount=total_amt
                )
            )

            create_mapping(
                db, "sales_receipt", invoice.id, qbo_id, _safe(qbo_sr, "SyncToken")
            )

            # Same inventory treatment as QBO-imported invoices
            db.flush()
            db.refresh(invoice)
            from app.services.inventory_hooks import post_sale_for_invoice

            post_sale_for_invoice(
                db,
                invoice,
                txn_date=invoice.date,
                post_journal=ledger_posting(db, "Sales Receipt", qbo_id) is None,
            )

            imported += 1
            qbo_progress.created()

        except Exception as e:
            qbo_progress.append_error(
                errors,
                {
                    "entity": "sales_receipt",
                    "qbo_id": str(qbo_id),
                    "code": getattr(e, "error_code", None)
                    or (
                        "IMPORT_VALIDATION"
                        if isinstance(e, DataProblem)
                        else "IMPORT_OPERATION_FAILED"
                    ),
                    "document_number": _safe(qbo_sr, "DocNumber")
                    or _safe(qbo_sr, "PaymentRefNum"),
                    "message": _source_context("sales_receipt", qbo_sr)
                    + ": "
                    + qbo_progress.error_message(e, "QBO import"),
                },
            )

    return {"imported": imported, "errors": errors}


# ============================================================================
# Journal entries
# ============================================================================


def _source_context(entity, source):
    source_id = _safe(source, "Id", "(missing ID)")
    document = _safe(source, "DocNumber") or _safe(source, "PaymentRefNum")
    context = f"{entity} QBO #{source_id}" + (
        f" (document {document})" if document else ""
    )
    linked = sorted(
        {
            f"{_safe(link, 'TxnType', '(missing type)')} QBO #{_safe(link, 'TxnId', '(missing ID)')}"
            for line in _safe(source, "Line", [])
            for link in _safe(line, "LinkedTxn", [])
        }
    )
    return context + (f"; linked {', '.join(linked)}" if linked else "")


def _journal_lines(qbo_entry, accounts) -> list[dict]:
    """Use PostingType, not account type or JournalEntry.TotalAmt (always 0)."""
    context = _source_context("JournalEntry", qbo_entry)
    rate = getattr(qbo_entry, "ExchangeRate", None)
    try:
        exchange_rate = Decimal(str(rate if rate is not None else 1))
    except InvalidOperation as exc:
        raise DataProblem(f"{context}: invalid exchange rate {rate!r}") from exc
    if not exchange_rate.is_finite() or exchange_rate <= 0:
        raise DataProblem(f"{context}: invalid exchange rate {rate!r}")

    lines = []
    posting_lines = 0
    for index, line in enumerate(_safe(qbo_entry, "Line", [])):
        detail_type = _safe(line, "DetailType", "")
        if detail_type in {"DescriptionOnly", "DescriptionOnlyLineDetail"}:
            continue
        detail = _safe(line, "JournalEntryLineDetail")
        account_ref = _safe(detail, "AccountRef")
        account_id = str(_safe(account_ref, "value", ""))
        line_id = getattr(line, "Id", None)
        location = (
            f"{context}, line #{line_id if line_id is not None else '(missing ID)'} "
            f"(position {index + 1}), account QBO #{account_id or '(missing ID)'} "
            f"({_safe(account_ref, 'name', '(missing name)')})"
        )
        if detail_type != "JournalEntryLineDetail":
            raise DataProblem(f"{location}: unsupported line type {detail_type!r}")
        posting_lines += 1
        posting_type = _safe(detail, "PostingType", "")
        if posting_type not in {"Debit", "Credit"}:
            raise DataProblem(
                f"{location}: missing Debit/Credit posting type "
                f"(PostingType={posting_type!r}, Amount={getattr(line, 'Amount', None)!r})"
            )
        value = getattr(line, "Amount", None)
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise DataProblem(f"{location}: unreadable line amount {value!r}") from exc
        if not amount.is_finite() or amount < 0:
            raise DataProblem(
                f"{location}: line amount must be finite and non-negative; received {value!r}"
            )
        if amount == 0:
            continue  # QBO can return zeroed lines on voided entries.
        account = accounts.get(account_id)
        if account is None:
            raise DataProblem(
                f"{location}: no local account mapping; import QBO Accounts first"
            )
        lines.append(
            {
                "account_id": account.id,
                "debit": amount if posting_type == "Debit" else Decimal("0"),
                "credit": amount if posting_type == "Credit" else Decimal("0"),
                "description": str(_safe(line, "Description", ""))[:300],
            }
        )
    if posting_lines < 2:
        raise DataProblem(
            f"{context}: missing its debit and credit lines; received {posting_lines} posting line(s)"
        )
    # A journal balances in its own currency. Converted line by line, a
    # foreign one can come out a cent apart, so it converts the way every
    # foreign-currency posting does: each line at the rate, and the cent of
    # rounding on the largest line (currency.convert_lines).
    debits = sum((line["debit"] for line in lines), Decimal("0"))
    credits = sum((line["credit"] for line in lines), Decimal("0"))
    if debits != credits:
        raise DataProblem(
            f"{context}: does not balance; debit {debits:.2f}, credit {credits:.2f}, "
            f"difference {debits - credits:.2f}; no lines were imported"
        )
    from app.services.accounting import _q
    from app.services.currency import convert_lines

    if exchange_rate == 1:
        lines = [
            {**line, "debit": _q(line["debit"]), "credit": _q(line["credit"])}
            for line in lines
        ]
    else:
        lines = convert_lines(lines, exchange_rate)
    return [line for line in lines if line["debit"] or line["credit"]]


def _post_journal(db, qbo_entry, txn_date, lines):
    """A QBO journal's lines as a journal here."""
    from app.services.accounting import create_journal_entry

    qbo_id = str(_safe(qbo_entry, "Id", ""))
    return create_journal_entry(
        db,
        txn_date,
        _safe(qbo_entry, "PrivateNote") or f"QBO Journal Entry {qbo_id}",
        lines,
        source_type="qbo_journal",
        reference=str(_safe(qbo_entry, "DocNumber") or qbo_id)[:100],
    )


def _non_posting_journal(qbo_entry, client, txn_date):
    """Verify old empty QBO journal stubs against the posted General Ledger.

    A missing amount/sign on a financial line remains a validation error.
    Only a single empty account line, accompanied by description-only lines,
    can be skipped, and only when QBO's ledger confirms no monetary posting.
    """
    from app.services.qbo_ledger_import import _money, _report_periods

    source_lines = _safe(qbo_entry, "Line", [])
    posting = [
        line
        for line in source_lines
        if _safe(line, "DetailType")
        not in {"DescriptionOnly", "DescriptionOnlyLineDetail"}
    ]
    if (
        len(posting) != 1
        or len(source_lines) < 2
        or _safe(posting[0], "DetailType") != "JournalEntryLineDetail"
        or _safe(_safe(posting[0], "JournalEntryLineDetail"), "PostingType")
    ):
        return False
    # The SDK supplies Amount=0 when QBO omits this field.
    amount = getattr(posting[0], "Amount", None)
    try:
        if amount is not None and Decimal(str(amount)) != 0:
            return False
    except InvalidOperation:
        return False
    qbo_id = str(_safe(qbo_entry, "Id", ""))
    context = _source_context("JournalEntry", qbo_entry)
    qbo_progress.emit(
        "verify",
        f"{context}: verifying empty account line against General Ledger on {txn_date}",
    )
    try:
        for _, _, rows in _report_periods(client, txn_date, txn_date):
            for _, cells in rows:
                if str(cells[1].get("id", "")) == qbo_id and _money(
                    cells[6].get("value")
                ):
                    return False
    except Exception as exc:
        detail = qbo_progress.error_message(exc, "QBO empty journal verification")
        raise DataProblem(
            f"{context}: account line has zero/absent Amount and no PostingType; could not verify "
            f"whether it posts to General Ledger on {txn_date}: {detail}"
        ) from exc
    ref = _safe(_safe(posting[0], "JournalEntryLineDetail"), "AccountRef")
    qbo_progress.emit(
        "skip",
        f"{context}: line #{getattr(posting[0], 'Id', '(missing ID)')}, account QBO "
        f"#{_safe(ref, 'value', '(missing ID)')}, has zero/absent Amount and no PostingType; "
        f"no monetary posting found in QBO General Ledger on {txn_date}",
        level="warning",
        code="IMPORT_NON_POSTING_JOURNAL",
    )
    qbo_progress.skipped("Verified non-posting journal; no local transaction needed")
    return True


@qbo_progress.stage("journal_entries")
def import_journal_entries(db: Session) -> dict:
    """Query every JournalEntry page and post complete journals once.

    https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/journalentry
    The SDK sends SELECT * FROM JournalEntry STARTPOSITION n MAXRESULTS 100.
    This import works independently of General Ledger report availability.
    """
    from quickbooks.objects.journalentry import JournalEntry as QBOJournalEntry

    from app.services.closing_date import check_closing_date
    from app.services.qbo_documents import (
        amount_of,
        reverse_for_import,
        why_not_changed,
    )
    from app.services.qbo_ledger_import import _account_map

    errors = []
    try:
        client = get_qbo_client(db)
        qbo_entries = _all_qbo_objects(QBOJournalEntry, client)
        accounts = _account_map(db)
    except Exception as exc:
        return {
            "imported": 0,
            "errors": [
                {
                    "entity": "journal_entries",
                    "message": f"Failed to query QBO: {qbo_progress.error_message(exc, 'QBO journal import')}",
                }
            ],
        }

    ledger_mappings = {}
    for mapping in db.query(QBOMapping).filter_by(entity_type="ledger"):
        txn_type, separator, qbo_id = mapping.qbo_id.partition(":")
        if separator and is_journal_entry_type(txn_type):
            ledger_mappings.setdefault(qbo_id, []).append(mapping)

    pending = []
    token_updates = []
    changes = []  # QBO changed an imported journal: reverse it, post again
    gone = []  # QBO voided or deleted an imported journal: reverse it
    unapplied = []  # a QBO change that can't be applied here: said, never blocking
    seen = set()
    listed = True  # every journal QBO returned has a sound ID

    def not_applied(qbo_id, number, context, what, why):
        qbo_progress.append_error(
            unapplied,
            {
                "entity": "journal_entries",
                "qbo_id": qbo_id,
                "document_number": number,
                "code": "IMPORT_QBO_CHANGE_NOT_APPLIED",
                "message": (
                    f"{context} was {what} in QuickBooks Online after it was "
                    f"imported, and that was not applied here: {why}. The books "
                    "keep what was imported."
                ),
            },
        )

    for qbo_entry in qbo_entries:
        qbo_id = str(_safe(qbo_entry, "Id", ""))
        context = _source_context("JournalEntry", qbo_entry)
        number = _safe(qbo_entry, "DocNumber")
        qbo_progress.item(qbo_id, number)
        owned = []
        try:
            if not qbo_id or len(qbo_id) > 100 or qbo_id in seen:
                listed = False
                raise DataProblem(
                    f"{context}: missing, duplicate, or invalid journal entry ID"
                )
            seen.add(qbo_id)
            value = _safe(qbo_entry, "TxnDate", "")
            try:
                txn_date = date.fromisoformat(str(value))
            except ValueError as exc:
                raise DataProblem(
                    f"{context}: invalid transaction date {value!r}"
                ) from exc
            mapping = get_mapping_by_qbo_id(db, "journal_entry", qbo_id)
            legacy = ledger_mappings.get(qbo_id, [])
            if any(m.qbo_sync_token in HERE for m in [mapping, *legacy] if m):
                # Voided here: a later import leaves it as it is.
                kept_here(("journal", qbo_id), [mapping, *legacy])
                continue
            owned = [m for m in [mapping, *legacy] if m]
            if not owned and _non_posting_journal(qbo_entry, client, txn_date):
                continue
            lines = _journal_lines(qbo_entry, accounts)
            if owned and all(m.qbo_sync_token in NOT_OWNED for m in owned):
                # Voided or deleted in QBO earlier; posting again if it is back.
                if not lines:
                    qbo_progress.skipped("Still voided in QuickBooks Online")
                    continue
                check_closing_date(db, txn_date)
                changes.append((qbo_entry, txn_date, lines, None, mapping, legacy))
                qbo_progress.validated(qbo_id)
                continue
            txn = None
            repair = None
            local_id = mapping.slowbooks_id if mapping else None
            if legacy:
                local_ids = {m.slowbooks_id for m in legacy}
                if len(local_ids) != 1 or (
                    local_id is not None and local_id not in local_ids
                ):
                    raise DataProblem(
                        f"{context}: multiple existing ledger postings; local transaction IDs "
                        + ", ".join(
                            str(n)
                            for n in sorted(
                                local_ids | ({local_id} if local_id else set())
                            )
                        )
                    )
                local_id = local_id if local_id is not None else legacy[0].slowbooks_id
            if local_id is not None:
                txn = db.get(Transaction, local_id)
                if txn is None:
                    not_applied(
                        qbo_id,
                        number,
                        context,
                        "changed",
                        f"its posting here, local transaction #{local_id}, does not exist",
                    )
                    continue
                if not lines:
                    # Voided in QBO: every line now reads 0.00.
                    why = why_not_changed(db, txn)
                    if why:
                        not_applied(qbo_id, number, context, "voided", why)
                        continue
                    gone.append((qbo_id, number, context, txn, owned, VOIDED_IN_QBO))
                    qbo_progress.validated(qbo_id)
                    continue
                if not journal_posting_matches(txn, txn_date, lines):
                    repair = (
                        legacy_rollup_repair(txn, txn_date, lines, accounts)
                        if legacy
                        else None
                    )
                    if not repair:
                        why = why_not_changed(db, txn, txn_date)
                        if why:
                            not_applied(qbo_id, number, context, "changed", why)
                            continue
                        changes.append(
                            (qbo_entry, txn_date, lines, txn, mapping, legacy)
                        )
                        qbo_progress.validated(qbo_id)
                        continue
                    check_closing_date(db, txn_date)
                elif mapping:
                    # SyncToken also changes for non-posting metadata edits.
                    # Verify the financial posting before refreshing the token.
                    token_updates.append((mapping, _safe(qbo_entry, "SyncToken")))
                    qbo_progress.skipped(
                        f"Verified local transaction #{txn.id}; date and account amounts match QBO"
                    )
                    continue
            elif not lines:
                qbo_progress.skipped("Zero-value journal; no local transaction needed")
                continue
            else:
                check_closing_date(db, txn_date)
            pending.append((qbo_entry, txn_date, lines, txn, mapping, repair))
            qbo_progress.validated(qbo_id)
        except Exception as exc:
            detail = (
                str(exc.detail)
                if isinstance(exc, HTTPException)
                else qbo_progress.error_message(exc, "QBO journal import")
            )
            error = {
                "entity": "journal_entries",
                "qbo_id": qbo_id,
                "document_number": number,
                "code": getattr(exc, "error_code", "IMPORT_VALIDATION"),
                "message": (
                    f"{context}: {detail}" if context not in detail else detail
                ),
            }
            if owned:
                # Imported before: QBO's new version can't be applied here,
                # and the rest of the batch goes on.
                error["code"] = "IMPORT_QBO_CHANGE_NOT_APPLIED"
                error["message"] += " The books keep what was imported."
                qbo_progress.append_error(unapplied, error)
            else:
                qbo_progress.append_error(errors, error)

    # Deleted in QBO: a journal imported before that a complete, sound list of
    # QBO's journals no longer has. (The list is complete: any page that
    # failed to load stopped the import above.)
    owners = {}
    for mapping in db.query(QBOMapping).filter_by(entity_type="journal_entry"):
        owners.setdefault(mapping.qbo_id, []).append(mapping)
    for qbo_id, mappings in ledger_mappings.items():
        owners.setdefault(qbo_id, []).extend(mappings)
    missing = {
        qbo_id: mappings
        for qbo_id, mappings in owners.items()
        if qbo_id not in seen
        and not any(m.qbo_sync_token in NOT_OWNED for m in mappings)
    }
    if missing and listed and not (seen & owners.keys()):
        # None of the journals imported before is in QBO's list: another
        # QBO company connected, most likely. Nothing is taken as deleted.
        qbo_progress.emit(
            "verify",
            f"None of the {len(owners)} journal(s) imported before is in "
            "QuickBooks Online's list of journals (is this the company they "
            "came from?), so none of them was taken as deleted",
            level="warning",
            code="IMPORT_QBO_DELETE_NOT_APPLIED",
            item_id="",
        )
        listed = False
    if listed:
        for qbo_id, mappings in sorted(missing.items()):
            txn = db.get(Transaction, mappings[0].slowbooks_id)
            if txn is None:
                continue
            context = f"JournalEntry QBO #{qbo_id}" + (
                f" (document {txn.reference})"
                if txn.reference and txn.reference != qbo_id
                else ""
            )
            qbo_progress.item(qbo_id, txn.reference or "")
            why = why_not_changed(db, txn)
            if why:
                not_applied(qbo_id, txn.reference or "", context, "deleted", why)
                continue
            gone.append(
                (qbo_id, txn.reference or "", context, txn, mappings, DELETED_IN_QBO)
            )
            qbo_progress.validated(qbo_id)

    if errors:
        qbo_progress.emit(
            "block",
            f"Journal batch not posted: {len(errors)} validation error(s); "
            f"{len(pending)} validated journal(s) waiting. No journal repairs or new postings saved.",
            level="warning",
            code="IMPORT_BATCH_BLOCKED",
            item_id="",
        )
        return {"imported": 0, "errors": errors + unapplied}

    imported = 0
    qbo_id = ""
    try:
        with db.begin_nested():
            for qbo_entry, _, _, txn, _, repair in pending:
                if repair:
                    qbo_id = str(qbo_entry.Id)
                    qbo_progress.posting(qbo_id, _safe(qbo_entry, "DocNumber"))
                    apply_rollup_repair(txn, repair, accounts)
            db.flush()
            if pending or changes or gone:
                rebase_account_balances(db, accounts.values())
            for mapping, token in token_updates:
                mapping.qbo_sync_token = token
            for qbo_entry, txn_date, lines, txn, mapping, _ in pending:
                qbo_id = str(qbo_entry.Id)
                qbo_progress.posting(qbo_id, _safe(qbo_entry, "DocNumber"))
                if txn is None:
                    txn = _post_journal(db, qbo_entry, txn_date, lines)
                    imported += 1
                    qbo_progress.created(qbo_id)
                else:
                    txn.source_type = "qbo_journal"
                    qbo_progress.emit(
                        "update",
                        f"Reused local transaction #{txn.id}; pending commit",
                        item_id=qbo_id,
                    )
                if mapping:
                    mapping.qbo_sync_token = _safe(qbo_entry, "SyncToken")
                else:
                    create_mapping(
                        db,
                        "journal_entry",
                        txn.id,
                        qbo_id,
                        _safe(qbo_entry, "SyncToken"),
                    )
            for qbo_entry, txn_date, lines, old, mapping, legacy in changes:
                qbo_id = str(qbo_entry.Id)
                qbo_progress.posting(qbo_id, _safe(qbo_entry, "DocNumber"))
                if old is not None:
                    reverse_for_import(db, old)
                txn = _post_journal(db, qbo_entry, txn_date, lines)
                token = _safe(qbo_entry, "SyncToken")
                if mapping:
                    mapping.slowbooks_id, mapping.qbo_sync_token = txn.id, token
                else:
                    create_mapping(db, "journal_entry", txn.id, qbo_id, token)
                for ledger in legacy:
                    ledger.slowbooks_id, ledger.qbo_sync_token = txn.id, token
                was = (
                    f"its posting here (local #{old.id}, {amount_of(old.lines)}) was "
                    "reversed and "
                    if old is not None
                    else ""
                )
                qbo_progress.emit(
                    "update",
                    f"{_source_context('JournalEntry', qbo_entry)} changed in "
                    f"QuickBooks Online: {was}the new version was posted (local "
                    f"#{txn.id}, {amount_of(lines)})",
                    code="IMPORT_QBO_CHANGE_APPLIED",
                )
            for qbo_id, number, context, old, mappings, token in gone:
                qbo_progress.posting(qbo_id, number)
                reverse_for_import(db, old)
                for mapping in mappings:
                    mapping.qbo_sync_token = token
                how = "voided" if token == VOIDED_IN_QBO else "deleted"
                qbo_progress.emit(
                    "update",
                    f"{context} was {how} in QuickBooks Online: its posting here "
                    f"(local #{old.id}, {amount_of(old.lines)}) was reversed",
                    code=(
                        "IMPORT_QBO_VOID_APPLIED"
                        if how == "voided"
                        else "IMPORT_QBO_DELETE_APPLIED"
                    ),
                )
            db.flush()
    except Exception as exc:
        return {
            "imported": 0,
            "errors": [
                {
                    "entity": "journal_entries",
                    "qbo_id": qbo_id,
                    "message": f"JournalEntry QBO #{qbo_id or '(batch)'}: "
                    + qbo_progress.error_message(exc, "QBO journal import"),
                }
            ]
            + unapplied,
        }
    return {"imported": imported, "errors": unapplied}


# ============================================================================
# Master import orchestrator
# ============================================================================


def _add_errors(errors: list, more: list) -> None:
    """A step's errors, less any the run has already: a change both the
    document import and the ledger import leave says so in the same line
    (qbo_documents.paid_past_new_total), which the result carries once, as
    the run's log does."""
    seen = {(e.get("entity"), e.get("qbo_id"), e.get("message")) for e in errors}
    for error in more:
        key = (error.get("entity"), error.get("qbo_id"), error.get("message"))
        if key not in seen:
            seen.add(key)
            errors.append(error)


def import_all(db: Session) -> dict:
    """Import all entity types from QBO in dependency order.

    Returns counts of imported records and any errors.
    """
    result = {
        "accounts": 0,
        "customers": 0,
        "vendors": 0,
        "items": 0,
        "invoices": 0,
        "payments": 0,
        "sales_receipts": 0,
        "journal_entries": 0,
        "ledger": 0,
        "errors": [],
    }

    # 1. Accounts first (items reference income/expense accounts)
    r = import_accounts(db)
    result["accounts"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 2. Customers (invoices + payments reference customers)
    r = import_customers(db)
    result["customers"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 3. Vendors
    r = import_vendors(db)
    result["vendors"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 4. Items (invoice lines reference items)
    r = import_items(db)
    result["items"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 5. Invoices (payments reference invoices)
    r = import_invoices(db)
    result["invoices"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 6. Payments
    r = import_payments(db)
    result["payments"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 7. Sales receipts (self-contained invoice + payment pairs)
    r = import_sales_receipts(db)
    result["sales_receipts"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 8. Query journals directly so report failures cannot hide them.
    r = import_journal_entries(db)
    result["journal_entries"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    # 9. Post all QBO financial activity after the chart is mapped. These
    # report postings also cover the documents above; they must be posted once.
    from app.services.qbo_ledger_import import import_ledger

    r = import_ledger(db)
    result["ledger"] = r["imported"]
    _add_errors(result["errors"], r["errors"])

    db.commit()
    return result
