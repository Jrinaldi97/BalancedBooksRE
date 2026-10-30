"""Atomic supplier invoice recognition and application of an actual card payment."""

from datetime import date
from app.portfolio import models as m, schemas as s


def invoice_state(db, code):
    from app.portfolio.service import digest, values, get
    from app.portfolio.reports import read_snapshot, ledger

    read_snapshot(db)
    item = db.query(m.OpenItem).filter_by(code=code).first()
    if item is None:
        return {"code": code, "exists": False}
    applications = [
        values(row)
        for row in db.query(m.ItemApplication)
        .filter_by(item_id=item.id)
        .order_by(m.ItemApplication.id)
    ]
    rows_by_basis = {
        basis: [
            row
            for row in ledger(db, date.max, account_id=item.control_id, basis=basis)
            if row.get("item") == code
        ]
        for basis in ("accrual", "modified_cash_bank_posting")
    }
    result = {
        "code": code,
        "exists": True,
        "item": values(item),
        "invoice": get(db, m.Event, item.event_id).source.get("supplier_invoice"),
        "applications": applications,
        "remaining_cents": item.amount_cents
        - sum(row["amount_cents"] for row in applications),
        "effective_payable_cents": {
            basis: -sum(row["debit_cents"] - row["credit_cents"] for row in rows)
            for basis, rows in rows_by_basis.items()
        },
        "effective_payable_digest": digest(rows_by_basis),
    }
    result["state_digest"] = digest(result)
    return result


def prepare_invoice(db, component, issuer_date, actor):
    """Return the existing/new item after checking exact identity and capacity."""
    from app.portfolio import service as svc

    invoice = component["invoice"]
    day = date.fromisoformat(invoice["invoice_date"])
    if day > issuer_date:
        svc.fail(
            "invoice_timing",
            "Use a prepaid workflow for service invoiced after the card payment",
        )
    target = svc.scope(db, component["account_id"], component["entity_id"])
    if target.role not in {"expense", "cogs"}:
        svc.fail(
            "invoice_timing",
            "Invoice-date recognition applies to completed operating costs; funded deposits remain assets at funding",
        )
    svc.get(db, m.Party, invoice["supplier_id"])
    proof = invoice["source"]
    if (
        not db.query(m.DocumentLocation)
        .filter_by(document_id=proof["document_id"], sha256=proof["sha256"])
        .first()
    ):
        svc.fail("invoice_evidence", "Register the exact invoice source version first")
    identity = {
        key: value for key, value in invoice.items() if key != "expected_invoice_digest"
    }
    identity.update(
        {
            key: component.get(key)
            for key in ("entity_id", "account_id", "property_id", "unit_id")
        }
    )
    current = invoice_state(db, invoice["code"])
    amount = invoice["gross_cents"] - invoice["earned_discount_cents"]
    if current["exists"]:
        if invoice["expected_invoice_digest"] != current["state_digest"]:
            svc.fail(
                "stale_invoice",
                "Read invoice_state before applying another payment to this invoice",
            )
        if current["invoice"] != identity:
            svc.fail(
                "invoice_identity",
                "Invoice identity, amount, service dates or supporting source differ from the recorded invoice",
            )
        item = svc.get(db, m.OpenItem, current["item"]["id"])
        remaining = current["remaining_cents"]
        if current["effective_payable_cents"] != {
            "accrual": remaining,
            "modified_cash_bank_posting": 0,
        }:
            svc.fail(
                "invoice_balance",
                "Invoice applications and effective payable disagree; reconcile the existing correction first",
            )
        from app.portfolio.dated_balances import liability_capacity

        capacity = liability_capacity(
            db,
            item.control_id,
            issuer_date,
            lambda row: row.get("item") == item.code
            and all(
                row.get(key) == component.get(key) for key in ("property_id", "unit_id")
            ),
        )
        if capacity["minimum"]["accrual"] < component["amount_cents"]:
            svc.fail(
                "invoice_overpayment",
                "Card payment exceeds this invoice's dated payable capacity",
            )
        event_ids = []
    else:
        if invoice["expected_invoice_digest"] is not None:
            svc.fail("stale_invoice", "The expected invoice does not exist")
        dims = {key: component.get(key) for key in ("property_id", "unit_id")}
        dims["item"] = invoice["code"]
        payable = svc.control(db, target.entity_id, "payable")
        lines = [
            svc.line(component["account_id"], amount, **dims),
            svc.line(payable, -amount, **dims),
        ]
        result = svc.post(
            db,
            s.Posting(
                date=day,
                kind="card_supplier_invoice",
                description="Supplier invoice " + invoice["code"],
                rationale=component["evidence"],
                source={"supplier_invoice": identity},
                journals=[s.Journal(entity_id=target.entity_id, lines=lines)],
                adjustments=[
                    s.Adjustment(
                        entity_id=target.entity_id,
                        date=day,
                        reason="Remove unpaid supplier invoice from modified cash",
                        lines=[
                            svc.line(
                                row.account_id,
                                row.credit_cents - row.debit_cents,
                                **dims
                            )
                            for row in lines
                        ],
                    )
                ],
            ),
            actor,
        )
        item = m.OpenItem(
            code=invoice["code"],
            entity_id=target.entity_id,
            property_id=component.get("property_id"),
            event_id=result["event_id"],
            kind="expense",
            account_id=component["account_id"],
            control_id=payable,
            amount_cents=amount,
            date=day,
        )
        db.add(item)
        db.add(
            m.DocumentLink(
                document_id=proof["document_id"], event_id=result["event_id"]
            )
        )
        db.flush()
        remaining = amount
        event_ids = [result["event_id"]]
    if component["amount_cents"] > remaining:
        svc.fail(
            "invoice_overpayment",
            "Card component exceeds the invoice's unapplied balance",
        )
    component.update(
        invoice_item_id=item.id,
        invoice_control_id=item.control_id,
        invoice_item=item.code,
    )
    return event_ids


def payment_adjustments(components, issuer_date):
    from app.portfolio.service import line

    result = []
    for component in components:
        if not component.get("invoice_item_id"):
            continue
        dims = {key: component.get(key) for key in ("property_id", "unit_id")}
        dims["item"] = component["invoice_item"]
        amount = component["amount_cents"]
        result.append(
            s.Adjustment(
                entity_id=component["entity_id"],
                date=issuer_date,
                reason="Recognize supplier cost at actual card payment on modified cash",
                lines=[
                    line(component["account_id"], amount, **dims),
                    line(component["invoice_control_id"], -amount, **dims),
                ],
            )
        )
    return result
