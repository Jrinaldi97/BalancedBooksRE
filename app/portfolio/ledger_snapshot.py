"""Ephemeral immutable ledger inputs shared by exact dated projections."""

from collections import defaultdict
from copy import deepcopy
from app.models.accounts import Account
from app.models.transactions import Transaction, TransactionLine
from app.portfolio import models as m
from app.portfolio.service import cents, get


class LedgerSnapshot:
    """Load one bounded scope once; never cache this packet beyond its request.

    All classification/allocation revisions through the maximum cutoff are kept,
    so earlier cutoffs do not accidentally inherit a later effective revision.
    Returned row dictionaries are independent of these retained inputs.
    Revision reads stream only financial projection fields, excluding evidence
    prose and ORM instances; retained history is still complete for each scope.
    """

    def __init__(
        self,
        db,
        through,
        *,
        event_ids=None,
        entity_ids=None,
        account_id=None,
        adjustments=True
    ):
        query = (
            db.query(
                TransactionLine,
                Transaction,
                m.JournalScope,
                m.LineScope,
                m.AccountScope,
                Account,
            )
            .join(Transaction, Transaction.id == TransactionLine.transaction_id)
            .join(m.JournalScope, m.JournalScope.transaction_id == Transaction.id)
            .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
            .join(
                m.AccountScope, m.AccountScope.account_id == TransactionLine.account_id
            )
            .join(Account, Account.id == TransactionLine.account_id)
            .filter(Transaction.date <= through)
        )
        if event_ids is not None:
            query = query.filter(m.JournalScope.event_id.in_(event_ids))
        if entity_ids:
            query = query.filter(m.JournalScope.entity_id.in_(entity_ids))
        if account_id:
            query = query.filter(Account.id == account_id)
        scoped_lines = query.with_entities(TransactionLine.id)
        self.classifications = defaultdict(list)
        for row in (
            db.query(
                m.CashClassification.line_id,
                m.CashClassification.date,
                m.CashClassification.event_id,
                m.CashClassification.cash_flow,
            )
            .filter(
                m.CashClassification.date <= through,
                m.CashClassification.line_id.in_(scoped_lines),
            )
            .order_by(m.CashClassification.date, m.CashClassification.id)
            .yield_per(256)
        ):
            self.classifications[row.line_id].append(
                (row.date, row.event_id, row.cash_flow)
            )
        self.allocations = defaultdict(list)
        for row in (
            db.query(
                m.LineAllocation.line_id,
                m.LineAllocation.date,
                m.LineAllocation.event_id,
                m.LineAllocation.components,
            )
            .filter(
                m.LineAllocation.date <= through,
                m.LineAllocation.line_id.in_(scoped_lines),
            )
            .order_by(m.LineAllocation.date, m.LineAllocation.id)
            .yield_per(256)
        ):
            self.allocations[row.line_id].append(
                # Scalar JSON projection decodes a fresh value, independent of
                # the ORM identity map. Retain it without a second full copy.
                (row.date, row.event_id, row.components)
            )
        self.rows = []
        for ln, txn, js, dim, sc, acct in query.order_by(
            Transaction.date, Transaction.id, TransactionLine.id
        ):
            self.rows.append(
                {
                    "line_id": ln.id,
                    "classification_event_id": js.event_id,
                    "event_id": js.event_id,
                    "entity_id": js.entity_id,
                    "date": txn.date.isoformat(),
                    "account_id": acct.id,
                    "name": acct.name,
                    "type": acct.account_type.value,
                    "role": sc.role,
                    "cash_flow": dim.cash_flow,
                    "debit_cents": cents(ln.debit),
                    "credit_cents": cents(ln.credit),
                    "unit_id": dim.unit_id,
                    "property_id": dim.property_id,
                    "fund_id": dim.fund_id,
                    "counterparty_id": dim.counterparty_id,
                    "item": dim.item,
                    "opening": txn.source_type == "re_opening",
                    "virtual": False,
                }
            )
        self.rows_by_event = defaultdict(list)
        for ordinal, row in enumerate(self.rows):
            self.rows_by_event[row["event_id"]].append((ordinal, row))
        self.adjustments = []
        if adjustments:
            query = db.query(m.BasisAdjustment).filter(
                m.BasisAdjustment.date <= through
            )
            if event_ids is not None:
                query = query.filter(m.BasisAdjustment.event_id.in_(event_ids))
            if entity_ids:
                query = query.filter(m.BasisAdjustment.entity_id.in_(entity_ids))
            selected = list(query)
            account_ids = {
                ln["account_id"]
                for adjustment in selected
                for ln in adjustment.lines
                if not account_id or ln["account_id"] == account_id
            }
            accounts = (
                {
                    a.id: (a.name, a.account_type.value, scope.role)
                    for a, scope in db.query(Account, m.AccountScope)
                    .join(m.AccountScope, m.AccountScope.account_id == Account.id)
                    .filter(Account.id.in_(account_ids))
                }
                if account_ids
                else {}
            )
            for adjustment in selected:
                for ln in adjustment.lines:
                    if account_id and ln["account_id"] != account_id:
                        continue
                    if ln["account_id"] not in accounts:
                        # Preserve the existing explicit missing-account behavior.
                        get(db, Account, ln["account_id"])
                        get(db, m.AccountScope, ln["account_id"])
                    name, kind, role = accounts[ln["account_id"]]
                    self.adjustments.append(
                        {
                            **deepcopy(ln),
                            "line_id": None,
                            "event_id": adjustment.event_id,
                            "entity_id": adjustment.entity_id,
                            "date": adjustment.date.isoformat(),
                            "name": name,
                            "type": kind,
                            "role": role,
                            "opening": False,
                            "virtual": True,
                            "reason": adjustment.reason,
                        }
                    )

    def project_bases(self, through, *, event_ids=None):
        accrual = self.project(through, event_ids=event_ids)
        selected = set(event_ids) if event_ids is not None else None
        cutoff = through.isoformat()
        adjustments = [
            dict(row)
            for row in self.adjustments
            if row["date"] <= cutoff
            and (selected is None or row["event_id"] in selected)
        ]
        return {
            "accrual": accrual,
            "modified_cash_bank_posting": [dict(row) for row in accrual] + adjustments,
        }

    def project(self, through, *, event_ids=None, property_id=None, basis="accrual"):
        selected = set(event_ids) if event_ids is not None else None
        cutoff = through.isoformat()
        result = []
        originals = (
            self.rows
            if selected is None
            else [
                row
                for _, row in sorted(
                    pair for ident in selected for pair in self.rows_by_event[ident]
                )
            ]
        )
        for original in originals:
            if original["date"] > cutoff or (
                selected is not None and original["event_id"] not in selected
            ):
                continue
            row = dict(original)
            classifications = [
                x for x in self.classifications[row["line_id"]] if x[0] <= through
            ]
            classification = classifications[-1] if classifications else None
            allocations = [
                x for x in self.allocations[row["line_id"]] if x[0] <= through
            ]
            allocation = allocations[-1] if allocations else None
            if classification:
                row.update(
                    classification_event_id=classification[1],
                    cash_flow=classification[2],
                )
            if allocation:
                later_classification = (
                    classification and classification[:2] > allocation[:2]
                )
                components = [
                    {
                        **row,
                        **{k: v for k, v in component.items() if k != "amount_cents"},
                        "debit_cents": max(component["amount_cents"], 0),
                        "credit_cents": max(-component["amount_cents"], 0),
                        "allocation_event_id": allocation[1],
                        "cash_flow": (
                            classification[2]
                            if later_classification
                            else component.get("cash_flow")
                        ),
                        "classification_event_id": (
                            classification[1] if later_classification else allocation[1]
                        ),
                    }
                    for component in allocation[2]
                ]
            else:
                components = [row]
            result.extend(
                component
                for component in components
                if property_id is None or component.get("property_id") == property_id
            )
        if basis == "modified_cash_bank_posting":
            result.extend(
                dict(row)
                for row in self.adjustments
                if row["date"] <= cutoff
                and (selected is None or row["event_id"] in selected)
                and (not property_id or row.get("property_id") == property_id)
            )
        return result
