"""Single effective scope interpretation for reporting and posting controls.

Replacement components are whole-line allocations, not extra bank postings.
Only allocations effective at the requested cutoff are included.
"""

from app.portfolio import models as m

DIMENSIONS = (
    "property_id",
    "unit_id",
    "fund_id",
    "counterparty_id",
    "item",
    "cash_flow",
)


def effective_map(db, through=None, line_ids=None):
    if line_ids is not None and len(line_ids) > 500:
        result = {}
        for offset in range(0, len(line_ids), 500):
            result.update(effective_map(db, through, line_ids[offset : offset + 500]))
        return result
    query = db.query(m.LineAllocation)
    if through is not None:
        query = query.filter(m.LineAllocation.date <= through)
    if line_ids is not None:
        query = query.filter(m.LineAllocation.line_id.in_(line_ids))
    return {
        row.line_id: row
        for row in query.order_by(m.LineAllocation.date, m.LineAllocation.id)
    }


def components(line_id, amount, dim, replacements):
    replacement = replacements.get(line_id)
    if replacement:
        return replacement.components
    return [{"amount_cents": amount, **{key: getattr(dim, key) for key in DIMENSIONS}}]


def property_event_ids(db, property_id, through=None):
    """Use replacement scopes when filtering evidence by effective property."""
    from app.models.transactions import TransactionLine

    replacements = effective_map(db, through)
    base = (
        db.query(m.JournalScope.event_id)
        .join(
            TransactionLine,
            TransactionLine.transaction_id == m.JournalScope.transaction_id,
        )
        .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
    )
    original = base.filter(m.LineScope.property_id == property_id)
    if replacements:
        original = original.filter(~TransactionLine.id.in_(replacements))
    result = {ident for (ident,) in original.distinct()}
    selected = [
        ident
        for ident, replacement in replacements.items()
        if any(
            c.get("property_id") == property_id and c["amount_cents"]
            for c in replacement.components
        )
    ]
    if selected:
        result.update(
            ident
            for (ident,) in base.filter(TransactionLine.id.in_(selected)).distinct()
        )
        result.update(replacements[ident].event_id for ident in selected)
    return result
