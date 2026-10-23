"""Owner distributions held for external owners, using real observed cash only.

Sender equity and holder custody are separate accounting facts. Receipt-only
coverage gaps remain visible. Individual custody items, rather than a natural
account total or assumed FIFO, govern onward payouts.
"""

from collections import defaultdict
from datetime import date
import uuid

from app.portfolio import models as m, schemas as s
from app.portfolio.allocations import DIMENSIONS
from app.portfolio.cash_lineage import normalized_cash
from app.portfolio.observation_state import observation_state
from app.portfolio.reports import ledger, read_snapshot

BASES = ("accrual", "modified_cash_bank_posting")


def _leg(db, spec, entity_id, direction):
    from app.portfolio.service import get, scope, fail

    obs = get(db, m.BankObservation, spec.observation_id)
    bank = scope(db, obs.account_id, entity_id)
    review = get(db, m.ReviewItem, spec.review_id)
    state = observation_state(db, obs.id)
    if (
        bank.role != "bank"
        or obs.status != "posted"
        or obs.amount_cents * direction <= 0
    ):
        fail(
            "distribution_source",
            "Choose the holder receipt or sender withdrawal with its actual bank owner",
        )
    if (
        review.observation_id != obs.id
        or review.code != "suspense"
        or review.status != "open"
    ):
        fail("review_state", "Choose the observation's open purpose review")
    if state["state_digest"] != spec.expected_state_digest:
        fail("stale_state", "The source observation changed; refresh it")
    for basis in BASES:
        residuals = state["suspense_residuals"][basis]
        if sum(row["amount_cents"] for row in residuals) != -obs.amount_cents or any(
            row["entity_id"] != entity_id for row in residuals
        ):
            fail(
                "distribution_scope",
                "The full observation must remain unclassified on both bases",
            )
        economic = defaultdict(int)
        for row in ledger(db, date.max, basis=basis, event_ids=state["event_ids"]):
            if row["role"] != "bank" and not row["role"].startswith("suspense"):
                economic[
                    (
                        row["entity_id"],
                        row["account_id"],
                        *(row.get(k) for k in DIMENSIONS),
                    )
                ] += (
                    row["debit_cents"] - row["credit_cents"]
                )
        if any(economic.values()):
            fail(
                "distribution_scope",
                "Existing economic classifications require their own reviewed correction",
            )
    cash, _ = normalized_cash(db, obs, state)
    return obs, bank, review, state, cash


def _external_fund(db, fund_id, party_id, holder_id):
    from app.portfolio.service import get, fail

    party = get(db, m.Party, party_id)
    fund = get(db, m.Fund, fund_id)
    if party.entity_id is not None:
        fail(
            "external_owner",
            "This workflow holds distributions for real external owners; maintained owners need coordinated beneficiary books",
        )
    if fund.holder_entity_id != holder_id or fund.beneficiary_id != party_id:
        fail(
            "wrong_beneficiary",
            "The fund must identify the actual holder and exact owner",
        )
    if fund.purpose != "general" or fund.restriction != "unrestricted":
        fail(
            "restricted_funds",
            "Owner distributions require an unrestricted general beneficiary fund",
        )
    return fund


def _shares(db, data, amount):
    from app.portfolio.service import get, fail

    get(db, m.Entity, data.sender_entity_id)
    active = [
        row
        for row in db.query(m.Ownership).filter_by(
            entity_id=data.sender_entity_id, interest="economic"
        )
        if row.start <= data.distribution_date < (row.end or date.max)
    ]
    if any(row.share_ppm is None for row in active):
        fail(
            "ownership_incomplete",
            "Every active economic owner's share must be supported",
        )
    active = [row for row in active if row.share_ppm]
    supplied = sorted(data.beneficiaries, key=lambda row: row.party_id)
    if (
        len({row.party_id for row in supplied}) != len(supplied)
        or len({row.fund_id for row in supplied}) != len(supplied)
        or {row.ownership_id for row in supplied} != {row.id for row in active}
        or sum(row.share_ppm for row in active) != 1000000
    ):
        fail(
            "ownership_incomplete",
            "Supply the complete, unique, dated economic ownership totaling 100 percent",
        )
    records = {row.id: row for row in active}
    if any(
        records[row.ownership_id].owner_id != row.party_id
        or records[row.ownership_id].share_ppm != row.share_ppm
        for row in supplied
    ):
        fail(
            "ownership_mismatch",
            "The party and exact share must agree with the dated ownership record",
        )
    shares = []
    allocated = 0
    cumulative_share = 0
    for index, declared in enumerate(supplied):
        record = records[declared.ownership_id]
        if (
            record.owner_id != declared.party_id
            or record.share_ppm != declared.share_ppm
        ):
            fail(
                "ownership_mismatch",
                "The party and exact share must agree with the dated ownership record",
            )
        # Cumulative half-up rounding prevents a negative final share for tiny
        # amounts split many ways. Stable party IDs determine rounding ties.
        cumulative_share += declared.share_ppm
        expected = (
            amount - allocated
            if index == len(supplied) - 1
            else (amount * cumulative_share + 500000) // 1000000 - allocated
        )
        if expected < 0 or declared.amount_cents != expected:
            fail(
                "distribution_amount",
                "Amounts must match dated shares and deterministic final-owner rounding",
            )
        allocated += expected
        fund = _external_fund(
            db, declared.fund_id, declared.party_id, data.holder_entity_id
        )
        item = f"{data.code}:{declared.party_id}"
        if db.query(m.OpenItem).filter_by(code=item).first():
            fail(
                "duplicate_item",
                "This individual owner distribution item already exists",
            )
        shares.append(
            {
                **declared.model_dump(),
                "item": item,
                "property_id": fund.property_id,
                "unit_id": fund.unit_id,
            }
        )
    if allocated != amount:
        fail("distribution_amount", "Beneficiary amounts must equal the actual receipt")
    return shares


def _source_funds(db, cash, entity_id, permitted_external=()):
    from app.portfolio.service import get, fail

    for row in cash:
        if not row.get("fund_id"):
            continue
        fund = get(db, m.Fund, row["fund_id"])
        owner = get(db, m.Party, fund.beneficiary_id)
        if (
            fund.holder_entity_id != entity_id
            or fund.restriction != "unrestricted"
            or (
                fund.purpose != "unresolved"
                and owner.entity_id != entity_id
                and fund.id not in permitted_external
            )
        ):
            fail(
                "client_financing",
                "This source cannot consume another beneficiary's or restricted funds",
            )


def _history_available(db, bank_id, through, fund_ids):
    """Check each later accounting/allocation boundary after a backdated change."""
    from app.models.transactions import Transaction, TransactionLine
    from app.portfolio.service import get, fail

    funds = {i for i in fund_ids if i and get(db, m.Fund, i).purpose != "unresolved"}
    line_ids = db.query(TransactionLine.id).filter_by(account_id=bank_id)
    days = {through}
    days.update(
        day
        for (day,) in db.query(Transaction.date)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .filter(TransactionLine.account_id == bank_id, Transaction.date >= through)
        .distinct()
    )
    days.update(
        day
        for (day,) in db.query(m.LineAllocation.date)
        .filter(
            m.LineAllocation.line_id.in_(line_ids), m.LineAllocation.date >= through
        )
        .distinct()
    )
    days.update(
        row.date
        for row in db.query(m.BasisAdjustment).filter(m.BasisAdjustment.date >= through)
        if any(line["account_id"] == bank_id for line in row.lines)
    )
    for day in sorted(days):
        for basis in BASES:
            totals = defaultdict(int)
            for row in ledger(db, day, account_id=bank_id, basis=basis):
                if row["virtual"]:
                    fail(
                        "distribution_basis",
                        "Review bank basis overrides before changing custody attribution",
                    )
                totals[row.get("fund_id")] += row["debit_cents"] - row["credit_cents"]
            if any(totals[ident] < 0 for ident in funds):
                fail(
                    "fund_shortfall",
                    "The identified fund is unavailable at a relevant historical date",
                )


def _correct(db, leg, lines, source, actor, evidence):
    from app.portfolio.service import post_existing_pool

    obs, bank, review, state, _ = leg
    posting = s.Posting(
        date=obs.date,
        description="Owner distribution custody",
        rationale=evidence,
        source={"observation_id": obs.id, **source},
        journals=[s.Journal(entity_id=bank.entity_id, lines=lines)],
    )
    return post_existing_pool(db, posting, obs, review, state, actor)


def correct_distribution(db, data, actor):
    from app.portfolio.service import get, scope, fail, line

    if data.sender_entity_id == data.holder_entity_id:
        fail(
            "distribution_entities",
            "The sender and actual holder must be distinct entities",
        )
    receipt = _leg(db, data.receipt, data.holder_entity_id, 1)
    inc, holder, _, _, holder_cash = receipt
    if inc.date < data.distribution_date:
        fail(
            "distribution_date",
            "The receipt cannot precede the supported distribution date",
        )
    custody = scope(db, data.custody_account_id, data.holder_entity_id)
    if custody.role != "custody_liability":
        fail("custody_account", "Choose the holder's real custody liability control")
    shares = _shares(db, data, inc.amount_cents)
    _source_funds(db, holder_cash, holder.entity_id, {row["fund_id"] for row in shares})
    outgoing = None
    if data.withdrawal:
        outgoing = _leg(db, data.withdrawal, data.sender_entity_id, -1)
        out, sender, _, _, sender_cash = outgoing
        if (
            out.id == inc.id
            or out.amount_cents != -inc.amount_cents
            or out.date != data.distribution_date
        ):
            fail(
                "distribution_pair",
                "The declared source must be the actual equal withdrawal on the distribution date",
            )
        if (
            scope(db, data.distribution_account_id, sender.entity_id).role
            != "distribution"
        ):
            fail("distribution_account", "Use the sender's equity distribution account")
        _source_funds(db, sender_cash, sender.entity_id)
        if data.sender_own_fund_id:
            fund = get(db, m.Fund, data.sender_own_fund_id)
            if (
                fund.holder_entity_id != sender.entity_id
                or fund.purpose == "unresolved"
                or fund.restriction != "unrestricted"
                or get(db, m.Party, fund.beneficiary_id).entity_id != sender.entity_id
            ):
                fail(
                    "funding_owner", "The source fund must belong to the actual sender"
                )
        for row in sender_cash:
            if row.get("fund_id"):
                original = get(db, m.Fund, row["fund_id"])
                if original.purpose == "unresolved" and not data.sender_own_fund_id:
                    fail(
                        "funding_source",
                        "Identify the sender's actual own fund before distributing unresolved pooled cash",
                    )
                if (
                    original.purpose != "unresolved"
                    and data.sender_own_fund_id not in (None, original.id)
                ):
                    fail(
                        "funding_source",
                        "Do not relabel already identified source ownership",
                    )
    source = {
        "owner_distribution_version": 1,
        "mode": data.mode,
        "sender_entity_id": data.sender_entity_id,
        "holder_entity_id": data.holder_entity_id,
        "distribution_date": data.distribution_date.isoformat(),
        "beneficiaries": shares,
        "source_evidence": data.source_evidence,
        "funding_evidence": data.funding_evidence,
        "coverage_gap": data.coverage_gap,
    }
    results = []
    if outgoing:
        out, sender, _, _, sender_cash = outgoing
        desired = [
            line(
                out.account_id,
                row["debit_cents"] - row["credit_cents"],
                **{
                    **{k: row.get(k) for k in DIMENSIONS},
                    "fund_id": data.sender_own_fund_id or row.get("fund_id"),
                    "cash_flow": "financing",
                },
            )
            for row in sender_cash
        ]
        desired += [
            line(data.distribution_account_id, row["amount_cents"], item=row["item"])
            for row in shares
            if row["amount_cents"]
        ]
        results.append(
            _correct(
                db,
                outgoing,
                desired,
                {**source, "owner_distribution_leg": "sender"},
                actor,
                data.evidence,
            )
        )
        _history_available(
            db,
            out.account_id,
            out.date,
            {row.get("fund_id") for row in sender_cash} | {data.sender_own_fund_id},
        )
    desired = []
    for row in shares:
        if not row["amount_cents"]:
            continue
        dims = {k: row[k] for k in ("fund_id", "property_id", "unit_id")}
        desired += [
            line(inc.account_id, row["amount_cents"], **dims, cash_flow="financing"),
            line(
                data.custody_account_id, -row["amount_cents"], **dims, item=row["item"]
            ),
        ]
    holding = _correct(
        db,
        receipt,
        desired,
        {**source, "owner_distribution_leg": "holder"},
        actor,
        data.evidence,
    )
    results.append(holding)
    _history_available(
        db,
        inc.account_id,
        inc.date,
        {row.get("fund_id") for row in holder_cash}
        | {row["fund_id"] for row in shares},
    )
    items = []
    for row in shares:
        if row["amount_cents"]:
            item = m.OpenItem(
                code=row["item"],
                entity_id=holder.entity_id,
                property_id=row["property_id"],
                event_id=holding["event_id"],
                kind="owner_custody",
                account_id=data.custody_account_id,
                control_id=data.custody_account_id,
                amount_cents=row["amount_cents"],
                date=inc.date,
            )
            db.add(item)
            items.append(item)
    db.flush()
    observations = [inc] + ([outgoing[0]] if outgoing else [])
    event_ids = [result["event_id"] for result in results]
    key = db.info["portfolio_command_key"]
    coordination = m.Event(
        code="RE-"
        + uuid.uuid5(uuid.NAMESPACE_URL, key + ":owner-distribution").hex[:16],
        kind="owner_distribution_to_holder",
        date=max(obs.date for obs in observations),
        description="Coordinated owner distribution held for beneficiaries",
        rationale=data.evidence,
        actor=actor,
        source={
            **source,
            "observation_ids": [obs.id for obs in observations],
            "source_event_ids": [obs.event_id for obs in observations] + event_ids,
        },
    )
    db.add(coordination)
    db.flush()
    coverage_ids = []
    if data.mode == "receipt_only":
        flag = m.ReviewItem(
            code="source_coverage",
            entity_id=holder.entity_id,
            observation_id=inc.id,
            event_id=coordination.id,
            detail="Receipt-only owner distribution: sender bank and equity posting are not represented. "
            + data.coverage_gap,
        )
        db.add(flag)
        db.flush()
        coverage_ids.append(flag.id)
    _verify_movements(db, observations)
    item_states = [owner_custody_state(db, item.id) for item in items]
    return {
        "event_id": coordination.id,
        "event_ids": [*event_ids, coordination.id],
        "resolved_review_ids": [
            ident for result in results for ident in result["resolved_review_ids"]
        ],
        "open_coverage_review_ids": coverage_ids,
        "receipt_only": data.mode == "receipt_only",
        "sender_posting_created": outgoing is not None,
        "bank_cash_delta_cents": 0,
        "beneficiaries": shares,
        "custody_items": item_states,
        "rounding": "Cumulative half-up rounding in Party ID order; final owner receives the residual cent",
    }


def _verify_movements(db, observations):
    from app.portfolio.service import fail

    expected = defaultdict(int)
    for obs in observations:
        expected[(obs.account_id, obs.date.isoformat())] += obs.amount_cents
    state = observation_state(db, observations[0].id)
    if any(state["suspense_residuals"].values()):
        fail("distribution_residual", "The coordinated correction left scoped suspense")
    for basis in BASES:
        actual = defaultdict(int)
        for row in ledger(db, date.max, basis=basis, event_ids=state["event_ids"]):
            if row["role"] == "bank":
                actual[(row["account_id"], row["date"])] += (
                    row["debit_cents"] - row["credit_cents"]
                )
        if {k: v for k, v in actual.items() if v} != dict(expected):
            fail(
                "distribution_cash",
                "Actual bank movements changed during classification",
            )


def owner_custody_state(db, item_id):
    from app.portfolio.service import get, scope, values, digest, fail

    read_snapshot(db)
    item = get(db, m.OpenItem, item_id)
    event = get(db, m.Event, item.event_id)
    source = event.source or {}
    matches = [
        row for row in source.get("beneficiaries", []) if row.get("item") == item.code
    ]
    if (
        item.kind != "owner_custody"
        or source.get("owner_distribution_version") != 1
        or source.get("owner_distribution_leg") != "holder"
        or len(matches) != 1
    ):
        fail(
            "custody_item",
            "Choose an individual obligation created by the typed owner distribution workflow",
        )
    share = matches[0]
    fund = _external_fund(db, share["fund_id"], share["party_id"], item.entity_id)
    if (
        item.amount_cents != share["amount_cents"]
        or item.amount_cents <= 0
        or scope(db, item.control_id, item.entity_id).role != "custody_liability"
        or item.property_id != share["property_id"]
        or fund.property_id != share["property_id"]
        or fund.unit_id != share["unit_id"]
    ):
        fail("custody_item", "The item and immutable beneficiary obligation disagree")
    applications = [
        (application, get(db, m.Event, application.event_id))
        for application in db.query(m.ItemApplication)
        .filter_by(item_id=item.id)
        .order_by(m.ItemApplication.id)
    ]
    remaining = item.amount_cents - sum(row.amount_cents for row, _ in applications)
    if remaining < 0 or any(
        row.amount_cents <= 0 or applied.date < item.date
        for row, applied in applications
    ):
        fail(
            "custody_item_state",
            "Custody applications exceed or predate the individual obligation",
        )
    rows_by_basis = {}
    expected_key = (
        share["property_id"],
        share["unit_id"],
        fund.id,
        None,
        item.code,
        None,
    )
    for basis in BASES:
        rows = [
            row
            for row in ledger(db, date.max, account_id=item.control_id, basis=basis)
            if row.get("item") == item.code
        ]
        totals = defaultdict(int)
        for row in rows:
            totals[tuple(row.get(k) for k in DIMENSIONS)] += (
                row["debit_cents"] - row["credit_cents"]
            )
        if {k: n for k, n in totals.items() if n} != (
            {expected_key: -remaining} if remaining else {}
        ):
            fail(
                "custody_item_state",
                "Both-basis custody balances must agree with this beneficiary's exact applications",
            )
        rows_by_basis[basis] = rows
    line_ids = {
        row["line_id"]
        for rows in rows_by_basis.values()
        for row in rows
        if row["line_id"] is not None
    }
    allocations = (
        db.query(m.LineAllocation)
        .filter(m.LineAllocation.line_id.in_(line_ids))
        .order_by(m.LineAllocation.id)
        .all()
    )
    boundaries = {item.date} | {
        date.fromisoformat(row["date"])
        for rows in rows_by_basis.values()
        for row in rows
    }
    boundaries |= {event.date for _, event in applications} | {
        row.date for row in allocations
    }
    for day in sorted(boundaries):
        if day < item.date:
            continue
        held = item.amount_cents - sum(
            row.amount_cents for row, event in applications if event.date <= day
        )
        for basis in BASES:
            totals = defaultdict(int)
            for row in ledger(db, day, account_id=item.control_id, basis=basis):
                if row.get("item") == item.code:
                    totals[tuple(row.get(k) for k in DIMENSIONS)] += (
                        row["debit_cents"] - row["credit_cents"]
                    )
            if {k: amount for k, amount in totals.items() if amount} != (
                {expected_key: -held} if held else {}
            ):
                fail(
                    "custody_item_state",
                    "Historical custody must agree with this exact beneficiary and its dated applications",
                )
    payload = {
        "item": values(item),
        "fund": values(fund),
        "source": source,
        "applications": [
            {**values(row), "date": event.date.isoformat()}
            for row, event in applications
        ],
        "ledger": rows_by_basis,
        "allocation_versions": [values(row) for row in allocations],
    }
    return {
        "item_id": item.id,
        "code": item.code,
        "holder_entity_id": item.entity_id,
        "party_id": share["party_id"],
        "fund_id": fund.id,
        "date": item.date.isoformat(),
        "original_cents": item.amount_cents,
        "applied_cents": item.amount_cents - remaining,
        "remaining_cents": remaining,
        "item_digest": digest(payload),
        "source_observation_id": source["observation_id"],
    }


def correct_payout(db, data, actor):
    from app.portfolio.service import get, scope, fail, line

    obs = get(db, m.BankObservation, data.observation.observation_id)
    bank = scope(db, obs.account_id)
    leg = _leg(db, data.observation, bank.entity_id, -1)
    if (
        len({row.item_id for row in data.components}) != len(data.components)
        or sum(row.amount_cents for row in data.components) != -obs.amount_cents
    ):
        fail(
            "custody_payout_total",
            "Unique individual item applications must equal the actual full withdrawal",
        )
    validated = []
    for component in data.components:
        current = owner_custody_state(db, component.item_id)
        if current["item_digest"] != component.expected_item_digest:
            fail("stale_custody_item", "The beneficiary obligation changed; refresh it")
        if (
            current["party_id"] != component.party_id
            or current["fund_id"] != component.fund_id
            or current["holder_entity_id"] != bank.entity_id
        ):
            fail(
                "wrong_beneficiary",
                "The payment must identify the actual holder, beneficiary and individual item",
            )
        item = get(db, m.OpenItem, component.item_id)
        if obs.date < item.date or component.amount_cents > current["remaining_cents"]:
            fail(
                "custody_overpayment",
                "A payout cannot predate or exceed the exact remaining beneficiary obligation",
            )
        fund = get(db, m.Fund, component.fund_id)
        validated.append((component, item, fund))
    _source_funds(db, leg[-1], bank.entity_id, {row.fund_id for row in data.components})
    desired = []
    for component, item, fund in validated:
        dims = dict(fund_id=fund.id, property_id=fund.property_id, unit_id=fund.unit_id)
        desired += [
            line(
                obs.account_id, -component.amount_cents, **dims, cash_flow="financing"
            ),
            line(item.control_id, component.amount_cents, **dims, item=item.code),
        ]
    result = _correct(
        db,
        leg,
        desired,
        {
            "owner_custody_payout_version": 1,
            "applications": [row.model_dump() for row in data.components],
        },
        actor,
        data.evidence,
    )
    for component, item, _ in validated:
        db.add(
            m.ItemApplication(
                item_id=item.id,
                event_id=result["event_id"],
                amount_cents=component.amount_cents,
            )
        )
    db.flush()
    _history_available(
        db,
        obs.account_id,
        obs.date,
        {row.get("fund_id") for row in leg[-1]}
        | {row.fund_id for row in data.components},
    )
    _verify_movements(db, [obs])
    return {
        **result,
        "custody_items": [owner_custody_state(db, item.id) for _, item, _ in validated],
        "applied_cents": -obs.amount_cents,
        "bank_cash_delta_cents": 0,
    }
