"""In-kind gifts — posting and void.

DR the asset or expense each line is (the piano to Musical Instruments,
the paint to Program Supplies), CR In-Kind Contributions, both sides
tagged to the fund and grant, so the contribution lands in the fund and
the Statement of Activities shows it without any cash moving. Void =
reversing entry, like every other document.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.accounts import Account, AccountType
from app.models.in_kind import InKindGift, InKindGiftLine
from app.models.transactions import Transaction
from app.services.accounting import (
    _q,
    create_journal_entry,
    fit_line_memo,
    get_in_kind_income_account_id,
    reversing_lines,
)
from app.services.numbering import next_document_number

_DEBIT_TYPES = (AccountType.ASSET, AccountType.EXPENSE, AccountType.COGS)


def next_in_kind_number(db: Session) -> str:
    return next_document_number(db, InKindGift.number, prefix="IK-", first=1, pad=4)


def build_lines(db: Session, gift: InKindGift, lines: list) -> Decimal:
    """Attach document lines (resolving the default credit account) and
    return the total fair value."""
    income_id = get_in_kind_income_account_id(db)
    total = Decimal("0")
    for i, ln in enumerate(lines):
        debit = db.get(Account, ln.debit_account_id)
        if debit is None or debit.account_type not in _DEBIT_TYPES:
            raise ValueError(
                "The gift must land in an asset or expense account (what the property is)"
            )
        credit_id = ln.credit_account_id or income_id
        credit = db.get(Account, credit_id)
        if credit is None or credit.account_type != AccountType.INCOME:
            raise ValueError("The credit side must be an income account")
        amount = _q(Decimal(str(ln.quantity)) * Decimal(str(ln.fair_value)))
        total += amount
        gift.lines.append(
            InKindGiftLine(
                description=ln.description,
                quantity=ln.quantity,
                fair_value=ln.fair_value,
                amount=amount,
                debit_account_id=debit.id,
                credit_account_id=credit.id,
                class_id=ln.class_id,
                job_id=ln.job_id,
                line_order=i,
            )
        )
    return _q(total)


def post_in_kind_gift(db: Session, gift: InKindGift) -> Transaction | None:
    je_lines: list[dict] = []
    for ln in gift.lines:
        amt = _q(ln.amount)
        if amt == 0:
            continue
        dims = {
            "class_id": ln.class_id or gift.class_id,
            "job_id": ln.job_id or gift.job_id,
        }
        je_lines.append(
            {
                "account_id": ln.debit_account_id,
                "debit": amt,
                "credit": Decimal("0"),
                "description": fit_line_memo(ln.description),
                **dims,
            }
        )
        je_lines.append(
            {
                "account_id": ln.credit_account_id,
                "debit": Decimal("0"),
                "credit": amt,
                "description": fit_line_memo(f"In-kind: {ln.description}"),
                **dims,
            }
        )
    if not je_lines:
        # a gift with no stated value still exists as a document (the
        # acknowledgment describes it); nothing posts
        return None
    donor = gift.customer.name if gift.customer else "donor"
    txn = create_journal_entry(
        db,
        gift.date,
        f"In-kind gift {gift.number} - {donor}",
        je_lines,
        source_type="in_kind_gift",
        source_id=gift.id,
        reference=gift.number,
        class_id=gift.class_id,
        job_id=gift.job_id,
    )
    gift.transaction_id = txn.id
    return txn


def void_in_kind_gift(db: Session, gift: InKindGift) -> None:
    if gift.status == "void":
        raise ValueError("In-kind gift is already void")
    txn = gift.transaction
    if txn is not None:
        create_journal_entry(
            db,
            gift.date,
            f"VOID {txn.description or gift.number}",
            reversing_lines(txn.lines),
            source_type="in_kind_gift_void",
            source_id=gift.id,
            reference=gift.number,
            class_id=gift.class_id,
            job_id=gift.job_id,
        )
    gift.status = "void"
