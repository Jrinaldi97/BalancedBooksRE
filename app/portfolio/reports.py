"""Source-linked portfolio statements in integer cents, never stored balances."""

from collections import defaultdict
from app.models.accounts import Account
from app.models.transactions import Transaction, TransactionLine
from app.portfolio import models as m
from app.portfolio.service import cents, values, get, fail

PL = {"income", "expense", "cogs"}


def read_snapshot(db):
    # SQLite's legacy transaction mode does not begin on SELECT alone.
    # Pin all queries contributing to a packet to the same database snapshot.
    if db.bind.dialect.name == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")


def ledger(
    db,
    end,
    entity_ids=None,
    property_id=None,
    account_id=None,
    basis="accrual",
    event_ids=None,
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
        .join(m.AccountScope, m.AccountScope.account_id == TransactionLine.account_id)
        .join(Account, Account.id == TransactionLine.account_id)
        .filter(Transaction.date <= end)
    )
    if event_ids is not None:
        query = query.filter(m.JournalScope.event_id.in_(event_ids))
    if entity_ids:
        query = query.filter(m.JournalScope.entity_id.in_(entity_ids))
    if account_id:
        query = query.filter(Account.id == account_id)
    # Keep date ordering identical while restricting override reads to this
    # ledger's actual line scope. The subquery avoids SQLite parameter limits
    # and includes pre-allocation lines before property filtering.
    scoped_line_ids = query.with_entities(TransactionLine.id)
    overrides = {
        x.line_id: x
        for x in db.query(m.CashClassification)
        .filter(
            m.CashClassification.date <= end,
            m.CashClassification.line_id.in_(scoped_line_ids),
        )
        .order_by(m.CashClassification.date, m.CashClassification.id)
    }
    rows = []
    for ln, txn, js, dim, sc, acct in query.order_by(
        Transaction.date, Transaction.id, TransactionLine.id
    ):
        rows.append(
            {
                "line_id": ln.id,
                "classification_event_id": (
                    overrides[ln.id].event_id if ln.id in overrides else js.event_id
                ),
                "event_id": js.event_id,
                "entity_id": js.entity_id,
                "date": txn.date.isoformat(),
                "account_id": acct.id,
                "name": acct.name,
                "type": acct.account_type.value,
                "role": sc.role,
                "cash_flow": (
                    overrides[ln.id].cash_flow if ln.id in overrides else dim.cash_flow
                ),
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
    from app.portfolio.allocations import effective_map

    replacements = effective_map(db, end, [row["line_id"] for row in rows])
    effective_rows = []
    for row in rows:
        replacement = replacements.get(row["line_id"])
        if replacement:
            for component in replacement.components:
                amount = component["amount_cents"]
                classification = overrides.get(row["line_id"])
                later_classification = classification and (
                    classification.date,
                    classification.event_id,
                ) > (replacement.date, replacement.event_id)
                effective_rows.append(
                    {
                        **row,
                        **{k: v for k, v in component.items() if k != "amount_cents"},
                        "debit_cents": max(amount, 0),
                        "credit_cents": max(-amount, 0),
                        "allocation_event_id": replacement.event_id,
                        "cash_flow": (
                            classification.cash_flow
                            if later_classification
                            else component.get("cash_flow")
                        ),
                        "classification_event_id": (
                            classification.event_id
                            if later_classification
                            else replacement.event_id
                        ),
                    }
                )
        else:
            effective_rows.append(row)
    rows = [
        row
        for row in effective_rows
        if property_id is None or row.get("property_id") == property_id
    ]
    if basis == "modified_cash_bank_posting":
        query = db.query(m.BasisAdjustment).filter(m.BasisAdjustment.date <= end)
        if event_ids is not None:
            query = query.filter(m.BasisAdjustment.event_id.in_(event_ids))
        if entity_ids:
            query = query.filter(m.BasisAdjustment.entity_id.in_(entity_ids))
        for adjustment in query:
            for ln in adjustment.lines:
                if property_id and ln.get("property_id") != property_id:
                    continue
                if account_id and ln["account_id"] != account_id:
                    continue
                acct = get(db, Account, ln["account_id"])
                sc = get(db, m.AccountScope, acct.id)
                rows.append(
                    {
                        **ln,
                        "line_id": None,
                        "event_id": adjustment.event_id,
                        "entity_id": adjustment.entity_id,
                        "date": adjustment.date.isoformat(),
                        "name": acct.name,
                        "type": acct.account_type.value,
                        "role": sc.role,
                        "opening": False,
                        "virtual": True,
                        "reason": adjustment.reason,
                    }
                )
    return rows


def statements(
    db, start, end, entity_ids=None, property_id=None, basis="accrual", group_id=None
):
    read_snapshot(db)
    if basis not in ("accrual", "modified_cash_bank_posting"):
        fail("report_basis", "Unsupported financial statement basis")
    if start > end:
        fail("date_range", "Report end precedes start")
    report_group = get(db, m.ReportingGroup, group_id) if group_id else None
    if report_group:
        if entity_ids or property_id:
            fail(
                "group_scope",
                "Choose the approved group alone; property and entity filters would change its perimeter",
            )
        if start < report_group.start or (report_group.end and end > report_group.end):
            fail(
                "group_dates",
                "Report period must be within the approved dated perimeter",
            )
        entity_ids = [
            x.entity_id
            for x in db.query(m.GroupMember).filter_by(group_id=report_group.id)
        ]
    selected = entity_ids or [e.id for e in db.query(m.Entity)]
    for ident in selected:
        get(db, m.Entity, ident)
    if property_id:
        get(db, m.Property, property_id)
    rows = ledger(db, end, selected, property_id, basis=basis)
    elimination_rows = []
    if len(selected) > 1:
        for row in list(rows):
            eliminate = (
                row["role"] in ("due_from", "due_to")
                and row.get("counterparty_id") in selected
            )
            if row["role"] in ("custody_asset", "custody_liability") and row.get(
                "fund_id"
            ):
                fund = get(db, m.Fund, row["fund_id"])
                party = get(db, m.Party, fund.beneficiary_id)
                eliminate = (
                    fund.holder_entity_id in selected and party.entity_id in selected
                )
            if eliminate:
                eliminated = {
                    **row,
                    "debit_cents": row["credit_cents"],
                    "credit_cents": row["debit_cents"],
                    "virtual": True,
                    "reason": "Reciprocal balance elimination within selected entities",
                }
                elimination_rows.append(eliminated)
        rows += elimination_rows
    group_adjustments = []
    group_cash_events = set()
    if report_group:
        for adjustment in db.query(m.GroupAdjustment).filter(
            m.GroupAdjustment.group_id == report_group.id, m.GroupAdjustment.date <= end
        ):
            if adjustment.basis not in ("both", basis):
                continue
            group_adjustments.append(values(adjustment))
            group_cash_events.update(adjustment.cash_flow_event_ids)
            for ln in adjustment.lines:
                acct = get(db, Account, ln["account_id"])
                sc = get(db, m.AccountScope, acct.id)
                rows.append(
                    {
                        **ln,
                        "event_id": -adjustment.id,
                        "entity_id": sc.entity_id,
                        "date": adjustment.date.isoformat(),
                        "name": acct.name,
                        "type": acct.account_type.value,
                        "role": sc.role,
                        "opening": False,
                        "virtual": True,
                        "reason": adjustment.reason,
                    }
                )
    issues = []
    balances = defaultdict(int)
    begin = defaultdict(int)
    period = defaultdict(int)
    meta = {}
    flow = defaultdict(int)
    noncash = []
    for row in rows:
        key = row["account_id"]
        value = row["debit_cents"] - row["credit_cents"]
        meta[key] = row
        balances[key] += value
        if row["date"] < start.isoformat() or (
            row["opening"] and row["date"] == start.isoformat()
        ):
            begin[key] += value
        else:
            period[key] += value
            if row["opening"]:
                issues.append(
                    "Opening positions begin inside the reporting period; earlier history is incomplete"
                )
            if row["role"] in ("bank", "cash_equivalent"):
                group = row.get("cash_flow")
                event = get(
                    db, m.Event, row.get("classification_event_id", row["event_id"])
                )
                if (
                    event.kind == "transfer"
                    and event.source.get("counterparty_entity_id") in selected
                    and len(selected) > 1
                ):
                    group = "internal"
                if row["event_id"] in group_cash_events:
                    group = "internal"
                if group == "internal":
                    # Within a single entity internal bank movements cancel;
                    # timing differences remain explicit and reconcile to cash.
                    flow["internal_transit"] += value
                elif group in ("operating", "investing", "financing"):
                    flow[group] += value
                else:
                    flow["unclassified"] += value
                    if value:
                        issues.append(
                            f"Cash flow classification missing on event {row['event_id']}"
                        )

    def totals(source, types):
        return sum(v for key, v in source.items() if meta[key]["type"] in types)

    ni = -totals(period, PL)
    cumulative = -totals(balances, PL)
    beginning_ni = -totals(begin, PL)
    assets = totals(balances, {"asset"})
    liabilities = -totals(balances, {"liability"})
    equity = -totals(balances, {"equity"}) + cumulative
    beginning_equity = -totals(begin, {"equity"}) + beginning_ni
    contributions = -sum(
        v for k, v in period.items() if meta[k]["role"] == "contribution"
    )
    distributions = sum(
        v for k, v in period.items() if meta[k]["role"] == "distribution"
    )
    other = -sum(
        v
        for k, v in period.items()
        if meta[k]["type"] == "equity"
        and meta[k]["role"] not in ("contribution", "distribution")
    )
    cash_start = sum(
        v for k, v in begin.items() if meta[k]["role"] in ("bank", "cash_equivalent")
    )
    cash_end = sum(
        v for k, v in balances.items() if meta[k]["role"] in ("bank", "cash_equivalent")
    )
    if assets - liabilities - equity:
        issues.append(
            "Property/entity attribution does not produce a balanced statement"
        )
    banks = (
        db.query(m.AccountScope)
        .filter(m.AccountScope.entity_id.in_(selected), m.AccountScope.role == "bank")
        .all()
        if selected
        else []
    )
    for bank in banks:
        if not bank.coverage_start:
            issues.append(
                f"Bank account {bank.account_id} has no supported opening coverage date"
            )
        elif bank.coverage_start > start:
            issues.append(
                f"Bank account {bank.account_id} history begins {bank.coverage_start}"
            )
    reviews = (
        db.query(m.ReviewItem)
        .filter(m.ReviewItem.entity_id.in_(selected), m.ReviewItem.status == "open")
        .all()
        if selected
        else []
    )
    if reviews:
        issues.append(
            f"{len(reviews)} open review items; assess materiality before issuing statements"
        )
    # Explicitly disclose noncash events instead of treating movements in debt
    # or fixed assets as proof of investing or financing cash flows.
    events = defaultdict(list)
    for row in rows:
        if (
            start.isoformat() <= row["date"]
            and not row["opening"]
            and not row["virtual"]
        ):
            events[row["event_id"]].append(row)
    for ident, lines in events.items():
        if not any(x["role"] in ("bank", "cash_equivalent") for x in lines) and any(
            x["role"] in ("fixed_asset", "project", "debt") for x in lines
        ):
            noncash.append({"event_id": ident, "lines": lines})

    def account_rows(source, types):
        return [
            {
                "account_id": k,
                "entity_id": meta[k]["entity_id"],
                "name": meta[k]["name"],
                "type": meta[k]["type"],
                "amount_cents": (
                    v if meta[k]["type"] in ("asset", "expense", "cogs") else -v
                ),
            }
            for k, v in sorted(source.items())
            if v and meta[k]["type"] in types
        ]

    # A source-linked bridge keeps bank-management results distinct from both formal bases.
    basis_bridge = []
    adjustment_query = db.query(m.BasisAdjustment).filter(
        m.BasisAdjustment.entity_id.in_(selected),
        m.BasisAdjustment.date >= start,
        m.BasisAdjustment.date <= end,
    )
    for adjustment in adjustment_query:
        difference = 0
        for ln in adjustment.lines:
            if property_id and ln.get("property_id") != property_id:
                continue
            if get(db, Account, ln["account_id"]).account_type.value in PL:
                difference -= ln["debit_cents"] - ln["credit_cents"]
        if difference:
            basis_bridge.append(
                {
                    "event_id": adjustment.event_id,
                    "reason": adjustment.reason,
                    "net_income_difference_cents": difference,
                }
            )
    if report_group:
        for adjustment in db.query(m.GroupAdjustment).filter(
            m.GroupAdjustment.group_id == report_group.id,
            m.GroupAdjustment.date >= start,
            m.GroupAdjustment.date <= end,
        ):
            if adjustment.basis == "both":
                continue
            change = -sum(
                ln["debit_cents"] - ln["credit_cents"]
                for ln in adjustment.lines
                if get(db, Account, ln["account_id"]).account_type.value in PL
            )
            basis_bridge.append(
                {
                    "group_adjustment_id": adjustment.id,
                    "reason": adjustment.reason,
                    "net_income_difference_cents": (
                        change
                        if adjustment.basis == "modified_cash_bank_posting"
                        else -change
                    ),
                }
            )
    difference = sum(x["net_income_difference_cents"] for x in basis_bridge)
    management_query = db.query(m.ManagementAllocation).filter(
        m.ManagementAllocation.entity_id.in_(selected),
        m.ManagementAllocation.date >= start,
        m.ManagementAllocation.date <= end,
    )
    if property_id:
        management_query = management_query.filter_by(property_id=property_id)
    management_net = sum(x.amount_cents for x in management_query)
    checks = {
        "balance_sheet_difference_cents": assets - liabilities - equity,
        "equity_rollforward_difference_cents": equity
        - (beginning_equity + ni + contributions - distributions + other),
        "cash_flow_difference_cents": cash_end - cash_start - sum(flow.values()),
        "trial_balance_difference_cents": sum(balances.values()),
    }
    return {
        "basis": basis,
        "basis_label": (
            "Accrual"
            if basis == "accrual"
            else "Modified cash basis—bank-posting settlement convention"
        ),
        "scope_label": (
            ("Consolidated reporting group: " + report_group.name)
            if report_group
            else (
                "Property segment financial statements"
                if property_id
                else (
                    "Combined entity statements with reciprocal balance eliminations; not GAAP consolidation"
                    if len(selected) > 1
                    else "Entity statements"
                )
            )
        ),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "entity_ids": selected,
        "property_id": property_id,
        "income_statement": {"lines": account_rows(period, PL), "net_income_cents": ni},
        "balance_sheet": {
            "lines": account_rows(balances, {"asset", "liability", "equity"}),
            "assets_cents": assets,
            "liabilities_cents": liabilities,
            "accumulated_income_cents": cumulative,
            "equity_cents": equity,
        },
        "changes_in_equity": {
            "beginning_cents": beginning_equity,
            "net_income_cents": ni,
            "contributions_cents": contributions,
            "distributions_cents": distributions,
            "other_identified_changes_cents": other,
            "ending_cents": equity,
        },
        "cash_flows": {
            "beginning_cents": cash_start,
            "sections_cents": dict(flow),
            "net_change_cents": sum(flow.values()),
            "ending_cents": cash_end,
            "noncash_events": noncash,
        },
        "checks": checks,
        "basis_reconciliation": {
            "accrual_net_income_cents": ni if basis == "accrual" else ni - difference,
            "modified_cash_net_income_cents": (
                ni + difference if basis == "accrual" else ni
            ),
            "bank_management_net_cents": management_net,
            "adjustments": basis_bridge,
            "note": "Bank management excludes retained noncash charges. Group-specific basis adjustments are listed separately.",
        },
        "readiness": "provisional",
        "limitations": sorted(
            set(
                issues
                + [
                    "Accounting policies, opening completeness and applicable disclosures require review before a GAAP-complete claim."
                ]
            )
        ),
        "eliminations": elimination_rows,
        "source_event_ids": sorted({r["event_id"] for r in rows if r["event_id"] > 0}),
        "group_id": group_id,
        "group_adjustments": group_adjustments,
        "policies": [
            values(p)
            for p in db.query(m.ReportingPolicy).filter(
                m.ReportingPolicy.entity_id.in_(selected),
                m.ReportingPolicy.start <= end,
            )
            if p.end is None or p.end >= start
        ],
    }


def overview(db, start, end, entity_id=None, property_id=None, bank_id=None):
    read_snapshot(db)
    ids = [entity_id] if entity_id else None
    rows = ledger(db, end, ids, property_id, bank_id)
    balances = defaultdict(int)
    meta = {}
    funds = defaultdict(int)
    intercompany = defaultdict(int)
    for row in rows:
        value = row["debit_cents"] - row["credit_cents"]
        meta[row["account_id"]] = row
        if row["role"] == "bank":
            balances[row["account_id"]] += value
            if row["fund_id"]:
                funds[row["fund_id"]] += value
        if row["role"] in ("due_from", "due_to"):
            intercompany[
                (row["entity_id"], row["counterparty_id"], row["item"], row["role"])
            ] += value
    q = db.query(m.ManagementAllocation).filter(
        m.ManagementAllocation.date >= start, m.ManagementAllocation.date <= end
    )
    if entity_id:
        q = q.filter_by(entity_id=entity_id)
    if property_id:
        q = q.filter_by(property_id=property_id)
    allocations = q.all()
    if bank_id:
        event_ids = {r["event_id"] for r in rows}
        allocations = [a for a in allocations if a.event_id in event_ids]
    reviewq = db.query(m.ReviewItem).filter_by(status="open")
    if entity_id:
        reviewq = reviewq.filter_by(entity_id=entity_id)
    if property_id or bank_id:
        event_ids = {r["event_id"] for r in rows}
        reviewq = reviewq.filter(m.ReviewItem.event_id.in_(event_ids))
    reviews = [values(r) for r in reviewq.order_by(m.ReviewItem.id).limit(100)]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "basis": "Bank-posting management",
        "bank_cash_cents": sum(balances.values()),
        "income_cents": sum(
            a.amount_cents
            for a in allocations
            if get(db, Account, a.account_id).account_type.value == "income"
        ),
        "expense_cents": -sum(
            a.amount_cents
            for a in allocations
            if get(db, Account, a.account_id).account_type.value in ("expense", "cogs")
        ),
        "banks": [
            {
                "account_id": k,
                "name": meta[k]["name"],
                "entity_id": meta[k]["entity_id"],
                "balance_cents": v,
            }
            for k, v in balances.items()
        ],
        "funds": [
            {
                "fund_id": k,
                "code": get(db, m.Fund, k).code,
                "held_cents": v,
                "restriction": get(db, m.Fund, k).restriction,
            }
            for k, v in funds.items()
        ],
        "intercompany": [
            {
                "entity_id": k[0],
                "counterparty_id": k[1],
                "item": k[2],
                "role": k[3],
                "balance_cents": v if k[3] == "due_from" else -v,
            }
            for k, v in intercompany.items()
            if v
        ],
        "review_items": reviews,
        "review_count": reviewq.count(),
        "note": "Bank cash counts physical banks once. Beneficiary holdings and Due From are not additional bank cash.",
    }


def event_detail(db, ident):
    event = get(db, m.Event, ident)
    lines = [
        {
            "line_id": ln.id,
            "entity_id": journal.entity_id,
            "account_id": ln.account_id,
            "account_name": acct.name,
            "debit_cents": cents(ln.debit),
            "credit_cents": cents(ln.credit),
            **values(dim),
        }
        for ln, journal, dim, acct in db.query(
            TransactionLine, m.JournalScope, m.LineScope, Account
        )
        .join(
            m.JournalScope,
            m.JournalScope.transaction_id == TransactionLine.transaction_id,
        )
        .join(m.LineScope, m.LineScope.line_id == TransactionLine.id)
        .join(Account, Account.id == TransactionLine.account_id)
        .filter(m.JournalScope.event_id == ident)
        .order_by(m.JournalScope.transaction_id, TransactionLine.id)
    ]
    document_rows = (
        db.query(m.DocumentLink, m.Document)
        .join(m.Document, m.Document.id == m.DocumentLink.document_id)
        .filter(m.DocumentLink.event_id == ident)
        .order_by(m.DocumentLink.id)
        .all()
    )
    locations = defaultdict(list)
    # A scoped join avoids per-document reads and large IN parameter lists.
    for location in (
        db.query(m.DocumentLocation)
        .filter(
            m.DocumentLocation.document_id.in_(
                db.query(m.DocumentLink.document_id).filter_by(event_id=ident)
            )
        )
        .order_by(m.DocumentLocation.id)
    ):
        locations[location.document_id].append(values(location))
    links = [
        {"document": values(doc), "page": link.page, "locations": locations[doc.id]}
        for link, doc in document_rows
    ]
    return {
        "event": values(event),
        "lines": lines,
        "documents": links,
        "management_allocations": [
            values(x)
            for x in db.query(m.ManagementAllocation).filter_by(event_id=ident)
        ],
        "cash_classifications": [
            values(x) for x in db.query(m.CashClassification).filter_by(event_id=ident)
        ],
        "line_allocations": [
            values(x) for x in db.query(m.LineAllocation).filter_by(event_id=ident)
        ],
        "item_applications": [
            values(x) for x in db.query(m.ItemApplication).filter_by(event_id=ident)
        ],
        "reviews_created": [
            values(x) for x in db.query(m.ReviewItem).filter_by(event_id=ident)
        ],
        "reviews_resolved": [
            values(x) for x in db.query(m.ReviewItem).filter_by(resolved_event_id=ident)
        ],
        "basis_adjustments": [
            values(x) for x in db.query(m.BasisAdjustment).filter_by(event_id=ident)
        ],
    }


def ownership_as_of(db, day, entity_id=None, property_id=None, unit_id=None):
    targets = [
        ("entity_id", entity_id),
        ("property_id", property_id),
        ("unit_id", unit_id),
    ]
    targets = [x for x in targets if x[1] is not None]
    if len(targets) != 1:
        fail("ownership_target", "Choose one entity, property or unit")
    active = (
        db.query(m.Ownership)
        .filter(
            m.Ownership.start <= day,
            (m.Ownership.end.is_(None)) | (m.Ownership.end > day),
            m.Ownership.interest == "economic",
        )
        .all()
    )
    paths = []
    issues = []

    def walk(key, ident, portion, path, visited):
        if (key, ident) in visited:
            issues.append("Circular ownership path; beneficial ownership is incomplete")
            return
        owners = [r for r in active if getattr(r, key) == ident]
        if sum(r.share_ppm or 0 for r in owners) != 1000000:
            issues.append(
                f"{key} {ident}: economic ownership is incomplete or includes unknown shares"
            )
        for row in owners:
            party = get(db, m.Party, row.owner_id)
            fraction = (
                None
                if portion is None or row.share_ppm is None
                else portion * row.share_ppm / 1000000
            )
            branch = path + [
                {
                    "ownership_id": row.id,
                    "party_id": party.id,
                    "name": party.name,
                    "share_ppm": row.share_ppm,
                }
            ]
            if party.entity_id and any(r.entity_id == party.entity_id for r in active):
                walk(
                    "entity_id",
                    party.entity_id,
                    fraction,
                    branch,
                    visited | {(key, ident)},
                )
            else:
                paths.append(
                    {
                        "owner_id": party.id,
                        "name": party.name,
                        "effective_share": fraction,
                        "path": branch,
                    }
                )

    walk(*targets[0], 1, [], set())
    return {
        "as_of": day.isoformat(),
        "paths": paths,
        "limitations": sorted(set(issues)),
        "note": "Ownership is descriptive. It does not allocate cash, expenses, debt or capital automatically.",
    }
