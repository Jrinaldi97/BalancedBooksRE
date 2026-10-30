"""Documents the QuickBooks Online import created, acted on here.

The QBO import brings invoices, sales receipts and payments in as documents
without a posting of their own. The ledger import (Posted Ledger Activity)
posts QBO's own lines for each one, as a journal mapped by its QBO
transaction ("Invoice:130", "Sales Receipt:132", "Payment:131").

A person here voids or edits such a document like any other:

- its import posting is reversed with it (the usual reversing entry, keyed
  by the transaction it reverses);
- an edit makes it an ordinary document of ours, with a posting of its own
  (the import's one reversed first, so A/R is never counted twice);
- the import mappings are marked CHANGED_HERE, so a later import leaves it
  alone instead of posting it again.
"""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.invoices import Invoice, InvoiceStatus
from app.models.items import InventoryMovement, Item, MovementType
from app.models.payments import Payment
from app.models.qbo_mapping import QBOMapping
from app.models.transactions import Transaction
from app.services.qbo_common import CHANGED_HERE, HERE, ledger_posting

# The QBO transaction type the ledger import posts each document kind under.
LEDGER_TYPE = {
    "invoice": "Invoice",
    "sales_receipt": "Sales Receipt",
    "payment": "Payment",
}
# Reversals keyed by the id of the transaction they reverse (the journal's
# own void, the house void_document() convention, a QBO import posting's).
REVERSALS = (
    "manual_void",
    "bank_entry_void",
    "deposit_void",
    "qbo_ledger_void",
    "qbo_journal_void",
)


def origin(db: Session, kinds, slowbooks_id) -> QBOMapping | None:
    """The QBO mapping of a local document, if it has one."""
    return (
        db.query(QBOMapping)
        .filter(
            QBOMapping.entity_type.in_(kinds),
            QBOMapping.slowbooks_id == slowbooks_id,
        )
        .first()
    )


def invoice_origin(db: Session, invoice) -> QBOMapping | None:
    """The mapping of an invoice or sales receipt the QBO import created
    (no posting of its own); None for one written here, even if exported."""
    if invoice.transaction_id is not None:
        return None
    return origin(db, ("invoice", "sales_receipt"), invoice.id)


def sales_receipt_of(db: Session, payment) -> Invoice | None:
    """The QBO sales receipt a payment is the payment half of (the import
    maps the receipt, not its payment)."""
    for alloc in payment.allocations:
        receipt = alloc.invoice
        if (
            receipt is not None
            and receipt.transaction_id is None
            and origin(db, ("sales_receipt",), receipt.id) is not None
        ):
            return receipt
    return None


def payment_origin(db: Session, payment) -> QBOMapping | None:
    """The mapping of a payment the QBO import created: its own, or its
    sales receipt's."""
    if payment.transaction_id is not None:
        return None
    mapping = origin(db, ("payment",), payment.id)
    if mapping is not None:
        return mapping
    receipt = sales_receipt_of(db, payment)
    return origin(db, ("sales_receipt",), receipt.id) if receipt else None


def reversed_already(db: Session, txn) -> bool:
    return (
        db.query(Transaction.id)
        .filter(
            Transaction.source_type.in_(REVERSALS),
            Transaction.source_id == txn.id,
        )
        .first()
        is not None
    )


def import_posting(db: Session, mapping: QBOMapping | None):
    """(ledger mapping, posting) the ledger import made for a QBO document,
    while that posting stands; None when the ledger import never posted it
    or it has been reversed."""
    if mapping is None or mapping.entity_type not in LEDGER_TYPE:
        return None
    ledger = ledger_posting(db, LEDGER_TYPE[mapping.entity_type], mapping.qbo_id)
    if ledger is None:
        return None
    txn = db.get(Transaction, ledger.slowbooks_id)
    if txn is None or reversed_already(db, txn):
        return None
    return ledger, txn


def mark_changed_here(db: Session, *mappings) -> None:
    """A later import leaves these alone (qbo_common.CHANGED_HERE). One an
    import has kept already stays KEPT_HERE: its line in the log was given
    the first time, and later runs count it with the others."""
    for mapping in mappings:
        if mapping is not None and mapping.qbo_sync_token not in HERE:
            mapping.qbo_sync_token = CHANGED_HERE


def reverse_import_posting(db: Session, txn, *, refusal: str):
    """Reverse an import posting with the usual reversing entry. `refusal`
    is the sentence for a posting on a reconciled bank statement, which
    stays as it is (a reconciled entry cannot be voided)."""
    from app.services.accounting import create_journal_entry, reversing_lines
    from app.services.bank_posting import release_statement_links

    if any(line.reconciliation_id for line in txn.lines):
        raise HTTPException(status_code=400, detail=refusal)
    reversal = create_journal_entry(
        db,
        txn.date,
        f"VOID {txn.description or ''}".strip(),
        reversing_lines(txn.lines),
        source_type=f"{txn.source_type}_void",
        source_id=txn.id,
        reference=txn.reference or "",
        class_id=txn.class_id,
        job_id=txn.job_id,
    )
    release_statement_links(db, txn)
    return reversal


# ---------------------------------------------------------------------------
# Stock a QBO document moved
# ---------------------------------------------------------------------------


def local_cost_live(db: Session, invoice) -> bool:
    """Does a local cost-of-goods entry stand for this QBO document's stock?

    The document import costs a sale here only while the ledger import has
    not posted it; once it has, QBO's posting carries the cost and the local
    entry is reversed (qbo_ledger_import._replace_import_cogs)."""
    for cost in db.query(Transaction).filter(
        Transaction.source_type.in_(("invoice", "invoice_edit")),
        Transaction.source_id == invoice.id,
    ):
        taken_back = (
            db.query(Transaction.id)
            .filter(
                Transaction.source_type == "qbo_cogs_void",
                Transaction.source_id == cost.id,
            )
            .first()
        )
        if cost.lines and not taken_back:
            return True
    return False


def restore_local_cost(db: Session, invoice) -> None:
    """Cost the stock a QBO document moved at the cost it moved at, for a
    document that becomes ours: its import posting, which carried QBO's
    cost of goods, has just been reversed."""
    from app.services.accounting import _q, create_journal_entry
    from app.services.inventory_service import (
        get_cogs_account_id,
        get_inventory_asset_account_id,
    )

    if local_cost_live(db, invoice):
        return
    for movement in db.query(InventoryMovement).filter(
        InventoryMovement.source_type == "invoice",
        InventoryMovement.source_id == invoice.id,
        InventoryMovement.movement_type == MovementType.SALE,
    ):
        item = db.get(Item, movement.item_id)
        amount = _q(
            abs(Decimal(str(movement.quantity))) * Decimal(str(movement.unit_cost or 0))
        )
        asset_id = get_inventory_asset_account_id(db, item) if item else None
        cogs_id = get_cogs_account_id(db)
        if amount <= 0 or not (asset_id and cogs_id):
            continue
        txn = create_journal_entry(
            db,
            invoice.date,
            f"COGS — {item.name}",
            [
                {
                    "account_id": cogs_id,
                    "debit": amount,
                    "credit": Decimal("0"),
                    "description": f"COGS: {item.name}",
                },
                {
                    "account_id": asset_id,
                    "debit": Decimal("0"),
                    "credit": amount,
                    "description": f"Inventory: {item.name}",
                },
            ],
            source_type="invoice",
            source_id=invoice.id,
        )
        movement.transaction_id = txn.id


# ---------------------------------------------------------------------------
# Voids
# ---------------------------------------------------------------------------


def void_invoice_import_posting(db: Session, invoice, what: str) -> bool:
    """For an invoice or sales receipt the QBO import created, reverse the
    posting the ledger import made for it and mark it changed here. Returns
    whether its stock was costed by that posting (so the stock goes back
    without a cost-of-goods entry of ours). A no-op for any other invoice."""
    mapping = invoice_origin(db, invoice)
    if mapping is None:
        return False
    carried = not local_cost_live(db, invoice)
    found = import_posting(db, mapping)
    ledger = None
    if found:
        ledger, txn = found
        reverse_import_posting(
            db,
            txn,
            refusal=(
                f"This {what}'s posting is on a reconciled bank statement, so it "
                "can't be voided."
            ),
        )
    mark_changed_here(db, mapping, ledger)
    return carried


def void_payment_import_posting(db: Session, payment) -> None:
    """For a payment the QBO import created, reverse the posting the ledger
    import made for it: the payment's own, or, for a sales receipt's
    payment, the receipt's (which carries the cash and the income; the
    receipt is voided next, and posts nothing of its own either)."""
    mapping = payment_origin(db, payment)
    if mapping is None:
        return
    found = import_posting(db, mapping)
    ledger = None
    if found:
        ledger, txn = found
        reverse_import_posting(
            db,
            txn,
            refusal=(
                "This payment is on a bank statement that has been reconciled, "
                "so it can't be voided. If the check bounced, charge the "
                "customer again with a new invoice."
            ),
        )
    mark_changed_here(db, mapping, ledger)


def money_in_posting(db: Session, payment):
    """Where a payment's money came in: its own posting, or the import
    posting of a payment the QBO import created (for the deposit check)."""
    if payment.transaction_id is not None:
        return None
    found = import_posting(db, payment_origin(db, payment))
    return found[1] if found else None


def document_of_posting(db: Session, txn):
    """(kind, document) when `txn` is the ledger import's posting of an
    invoice, sales receipt or payment the QBO import created and that still
    stands; None for any other posting (a journal, a purchase, a deposit)."""
    kinds = {kind: qbo_type for kind, qbo_type in LEDGER_TYPE.items()}
    for ledger in db.query(QBOMapping).filter(
        QBOMapping.entity_type == "ledger", QBOMapping.slowbooks_id == txn.id
    ):
        qbo_type, _, qbo_id = ledger.qbo_id.partition(":")
        wanted = qbo_type.lower().replace(" ", "")
        for kind, name in kinds.items():
            if name.lower().replace(" ", "") != wanted:
                continue
            mapping = (
                db.query(QBOMapping)
                .filter(QBOMapping.entity_type == kind, QBOMapping.qbo_id == qbo_id)
                .first()
            )
            if mapping is None:
                continue
            if kind == "payment":
                document = db.get(Payment, mapping.slowbooks_id)
                if document is not None and not document.is_voided:
                    return kind, document
            else:
                document = db.get(Invoice, mapping.slowbooks_id)
                if document is not None and document.status != InvoiceStatus.VOID:
                    return kind, document
    return None


# ---------------------------------------------------------------------------
# Edits: the document becomes ours
# ---------------------------------------------------------------------------


def post_payment_journal(db: Session, payment) -> None:
    """A payment's own posting (DR the account it went to / CR A/R), for the
    payment of a QBO sales receipt that becomes ours."""
    from app.models.contacts import Customer
    from app.services.accounting import (
        create_journal_entry,
        get_ar_account_id,
        get_undeposited_funds_id,
    )

    customer = db.get(Customer, payment.customer_id)
    amount = Decimal(str(payment.amount))
    txn = create_journal_entry(
        db,
        payment.date,
        f"Payment from {customer.name if customer else 'Unknown'}",
        [
            {
                "account_id": payment.deposit_to_account_id
                or get_undeposited_funds_id(db),
                "debit": amount,
                "credit": Decimal("0"),
                "description": "Payment received",
            },
            {
                "account_id": get_ar_account_id(db),
                "debit": Decimal("0"),
                "credit": amount,
                "description": "Payment received",
            },
        ],
        source_type="payment",
        source_id=payment.id,
        reference=payment.reference or "",
    )
    payment.transaction_id = txn.id


def adopt_invoice(db: Session, invoice) -> bool:
    """Make an invoice or sales receipt the QBO import created an ordinary
    one of ours, before an edit changes its amounts or date. Its import
    posting is reversed (the stock it moved is costed here again), a sales
    receipt's payment gets its posting, and the caller posts the invoice's
    own entry from its edited lines. Returns False for any other invoice."""
    mapping = invoice_origin(db, invoice)
    if mapping is None:
        return False
    what = "sales receipt" if mapping.entity_type == "sales_receipt" else "invoice"
    found = import_posting(db, mapping)
    ledger = None
    if found:
        ledger, txn = found
        reverse_import_posting(
            db,
            txn,
            refusal=(
                f"This {what}'s posting is on a reconciled bank statement, so its "
                "amounts and date can't be changed."
            ),
        )
    # Its stock is costed here from now on (a no-op while a cost of ours
    # stands: the ledger import never posted it).
    restore_local_cost(db, invoice)
    if mapping.entity_type == "sales_receipt":
        for alloc in invoice.payment_allocations:
            payment = alloc.payment
            if payment and not payment.is_voided and payment.transaction_id is None:
                post_payment_journal(db, payment)
    mark_changed_here(db, mapping, ledger)
    adopt_unposted_payments(db, invoice)
    return True


def adopt_unposted_payments(db: Session, invoice) -> None:
    """The QBO payments on an invoice that becomes ours, when they have no
    posting at all (the ledger import never posted them): ours too, and
    posted, so A/R is right at once (the invoice posts its own debit). A
    later ledger import keeps them (CHANGED_HERE).

    Such a payment may also pay other invoices the import made, with no
    posting either; those become ours with it, so every part of its credit
    to A/R meets an invoice's debit there. With the ledger import run, each
    payment already has its posting, and nothing here changes."""
    from app.routes.invoices.helpers import _post_invoice_journal

    invoices, payments = [invoice], set()
    seen = {invoice.id}
    while invoices:
        paid = invoices.pop()
        for alloc in list(paid.payment_allocations):
            payment = alloc.payment
            if payment is None or payment.id in payments or payment.is_voided:
                continue
            payments.add(payment.id)
            mapping = origin(db, ("payment",), payment.id)
            if (
                mapping is None
                or payment.transaction_id is not None
                or import_posting(db, mapping) is not None
            ):
                continue
            post_payment_journal(db, payment)
            mark_changed_here(db, mapping)
            for other in payment.allocations:
                sibling = other.invoice
                if sibling is None or sibling.id in seen:
                    continue
                seen.add(sibling.id)
                sibling_map = invoice_origin(db, sibling)
                if (
                    sibling_map is None
                    or sibling.status == InvoiceStatus.VOID
                    or import_posting(db, sibling_map) is not None
                ):
                    continue
                restore_local_cost(db, sibling)
                txn = _post_invoice_journal(
                    db,
                    sibling,
                    list(sibling.lines),
                    sibling.customer.name if sibling.customer else "",
                    balance_on_income=True,
                )
                sibling.transaction_id = txn.id
                mark_changed_here(db, sibling_map)
                invoices.append(sibling)


# ---------------------------------------------------------------------------
# Changes made in QuickBooks Online, applied by the import
# ---------------------------------------------------------------------------


def why_not_changed(db: Session, txn, new_date=None) -> str | None:
    """Why the import can't reverse its posting `txn` (and post a new
    version dated `new_date`), in words; None when it can."""
    from app.services.closing_date import get_closing_date

    closing = get_closing_date(db)
    if closing is not None:
        for day in (txn.date, new_date):
            if day is not None and day <= closing:
                return f"the books are closed through {closing}, and it is dated {day}"
    if any(line.reconciliation_id for line in txn.lines):
        return "one of its lines is on a reconciled bank statement here"
    if any(line.deposit_transaction_id for line in txn.lines):
        return "its money is in a deposit made here"
    return None


def reverse_for_import(db: Session, txn) -> Transaction:
    """The import reverses one of its own postings (QBO changed or voided
    it): the usual reversing entry, keyed by the posting."""
    from app.services.accounting import create_journal_entry, reversing_lines
    from app.services.bank_posting import release_statement_links

    reversal = create_journal_entry(
        db,
        txn.date,
        f"VOID {txn.description or ''}".strip(),
        reversing_lines(txn.lines),
        source_type=f"{txn.source_type}_void",
        source_id=txn.id,
        reference=txn.reference or "",
        class_id=txn.class_id,
        job_id=txn.job_id,
    )
    release_statement_links(db, txn)
    return reversal


def amount_of(lines) -> str:
    """ "25.54": what a posting moves (its debits), for the import log."""
    total = sum(
        (Decimal(str(line["debit"] if isinstance(line, dict) else line.debit)))
        for line in lines
    )
    return f"{total:,.2f}"


def void_document_from_qbo(db: Session, qbo_type: str, qbo_id: str) -> str | None:
    """QBO voided a transaction whose document the QBO import created: void
    that document here too (its import posting is reversed by the caller).
    A sales receipt's payment goes with it; a payment QBO leaves standing
    once the invoice it paid is voided stays, unapplied, as in QBO. Returns
    the document's name for the import log, or None if there is none to
    void (none, voided already, or ours now)."""
    from app.services.inventory_service import reverse_sale
    from app.services.qbo_common import VOIDED_IN_QBO

    kind = {
        "invoice": "invoice",
        "salesreceipt": "sales_receipt",
        "payment": "payment",
    }.get(qbo_type.lower().replace(" ", ""))
    if kind is None:
        return None
    mapping = (
        db.query(QBOMapping)
        .filter(QBOMapping.entity_type == kind, QBOMapping.qbo_id == qbo_id)
        .first()
    )
    if mapping is None:
        return None
    if kind == "payment":
        payment = db.get(Payment, mapping.slowbooks_id)
        if payment is None or payment.is_voided or payment.transaction_id is not None:
            return None
        for alloc in payment.allocations:
            unpay_invoice(alloc.invoice, alloc.amount)
        payment.is_voided = True
        mapping.qbo_sync_token = VOIDED_IN_QBO
        return f"payment #{payment.id}"
    invoice = db.get(Invoice, mapping.slowbooks_id)
    if (
        invoice is None
        or invoice.status == InvoiceStatus.VOID
        or invoice.transaction_id is not None
    ):
        return None
    cost_here = local_cost_live(db, invoice)
    for alloc in list(invoice.payment_allocations):
        payment = alloc.payment
        own = (
            kind == "sales_receipt"
            and payment is not None
            and payment.transaction_id is None
            and not payment.is_voided
        )
        if own:
            payment.is_voided = True  # the receipt's own payment goes with it
        else:
            db.delete(alloc)  # a payment of it stays, unapplied
    for line in invoice.lines:
        item = db.get(Item, line.item_id) if line.item_id else None
        if item is not None and item.track_inventory:
            reverse_sale(
                db,
                item,
                quantity=Decimal(str(line.quantity)),
                source_type="invoice_void",
                source_id=invoice.id,
                original_source_type="invoice",
                original_source_id=invoice.id,
                txn_date=invoice.date,
                post_journal=cost_here,
            )
    invoice.amount_paid = Decimal("0")
    invoice.balance_due = Decimal("0")
    invoice.status = InvoiceStatus.VOID
    mapping.qbo_sync_token = VOIDED_IN_QBO
    noun = "sales receipt" if kind == "sales_receipt" else "invoice"
    return f"{noun} {invoice.invoice_number}"


def current_invoice_for_import(db, invoice_id):
    """Refresh the paid-total guard in a long-lived import session.

    An invoice cached before a separate payment request must not hide that
    payment and permit importing a new total below it. Flush our own pending
    changes first, then lock and reload the authoritative row.
    """
    db.flush()
    return db.get(Invoice, invoice_id, populate_existing=True, with_for_update=True)


def paid_past_new_total(invoice, qbo_id, new_total) -> dict | None:
    """An invoice the QBO import made, changed in QuickBooks Online to a
    total below the payments recorded against it here. Neither the document
    import nor the ledger import applies that change, so the invoice and
    its posting stay in step (it would owe less than nothing). The one line
    both give for it, in the same words, so a run's log shows it once;
    None when the change can be applied."""
    paid = Decimal(str(invoice.amount_paid or 0))
    new_total = Decimal(str(new_total))
    if new_total >= paid:
        return None
    return {
        "entity": "invoice",
        "qbo_id": str(qbo_id),
        "document_number": invoice.invoice_number,
        "code": "IMPORT_QBO_CHANGE_NOT_APPLIED",
        "message": (
            f"Invoice QBO #{qbo_id} (document {invoice.invoice_number}) was changed "
            f"in QuickBooks Online to {new_total:,.2f}, but the payments recorded "
            f"against it here come to {paid:,.2f}, more than its new total. The "
            "invoice and its posting stay as they were imported; take a payment "
            "off it here, and the next import brings QuickBooks Online's change in."
        ),
    }


def closed_on(db: Session, day) -> str | None:
    """ "the books are closed through …" when `day` is in a closed period."""
    from app.services.closing_date import get_closing_date

    closing = get_closing_date(db)
    if closing is not None and day is not None and day <= closing:
        return f"the books are closed through {closing}, and it is dated {day}"
    return None


def pay_invoice(invoice, amount) -> None:
    """Put a payment's amount on an invoice, as recording it does."""
    invoice.amount_paid = (invoice.amount_paid or Decimal("0")) + amount
    invoice.balance_due = invoice.total - invoice.amount_paid
    invoice.status = (
        InvoiceStatus.PAID
        if invoice.balance_due <= 0
        else InvoiceStatus.PARTIAL if invoice.amount_paid > 0 else InvoiceStatus.SENT
    )


def unpay_invoice(invoice, amount) -> None:
    """Take a payment's amount off an invoice it paid, as a payment void does."""
    if invoice is None or invoice.status == InvoiceStatus.VOID:
        return
    invoice.amount_paid = (invoice.amount_paid or Decimal("0")) - amount
    invoice.balance_due = (invoice.balance_due or Decimal("0")) + amount
    if invoice.amount_paid > 0:
        invoice.status = InvoiceStatus.PARTIAL
    else:
        invoice.status = InvoiceStatus.SENT
