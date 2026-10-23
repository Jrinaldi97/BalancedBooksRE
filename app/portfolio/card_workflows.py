"""Source-preserving card costs, refunds and dated bank/issuer settlements."""

from collections import defaultdict
from datetime import date, timedelta
from app.portfolio import models as m, schemas as s
from app.portfolio.allocations import DIMENSIONS


def _svc():
    from app.portfolio import service

    return service


def _identity(db, ident):
    svc = _svc()
    card = svc.get(db, m.CardIdentity, ident)
    account = svc.scope(db, card.account_id, card.entity_id)
    if account.role != "credit_card":
        svc.fail(
            "card_account",
            "The stable card identity must use its company credit-card liability account",
        )
    return card


def register_card_identity(db, data, actor):
    svc = _svc()
    account = svc.scope(db, data.account_id, data.entity_id)
    if account.role != "credit_card":
        svc.fail("card_account", "Register an actual credit-card liability account")
    if (
        db.query(m.CardIdentity)
        .filter(
            (m.CardIdentity.account_id == data.account_id)
            | (m.CardIdentity.code == data.code)
        )
        .first()
    ):
        svc.fail(
            "card_identity",
            "This card account or stable identity is already registered",
        )
    row = m.CardIdentity(**data.model_dump(), actor=actor)
    row.issuer = row.issuer.strip().casefold()
    if not row.issuer:
        svc.fail("card_identity", "Issuer identity cannot be blank")
    db.add(row)
    db.flush()
    return {
        "card_id": row.id,
        "account_id": row.account_id,
        "financial_entries_added": False,
    }


def add_card_alias(db, data, actor):
    svc = _svc()
    card = _identity(db, data.card_id)
    alias = data.alias.strip().casefold()
    if not alias or (data.end and data.end < data.start):
        svc.fail("card_alias", "Provide a nonempty alias and a valid issuer interval")
    if db.query(m.CardAlias).filter_by(issuer=card.issuer, alias=alias).first():
        svc.fail(
            "card_alias",
            "An issuer alias cannot identify two cards or be silently reassigned",
        )
    row = m.CardAlias(
        **data.model_dump(exclude={"alias"}),
        alias=alias,
        issuer=card.issuer,
        actor=actor,
    )
    db.add(row)
    db.flush()
    return {
        "card_alias_id": row.id,
        "card_id": card.id,
        "financial_entries_added": False,
    }


def import_card_activity(db, data, actor):
    svc = _svc()
    card = _identity(db, data.card_id)
    alias = (
        db.query(m.CardAlias)
        .filter_by(
            card_id=card.id, issuer=card.issuer, alias=data.alias.strip().casefold()
        )
        .first()
    )
    if not alias or any(
        r.date < alias.start or (alias.end and r.date > alias.end) for r in data.rows
    ):
        svc.fail(
            "card_alias",
            "Every issuer row must fall within this verified alias interval",
        )
    if any(r.activity_status != "posted" for r in data.rows):
        svc.fail(
            "card_activity_status",
            "Pending and declined authorizations are not posted issuer activity",
        )
    result = svc.import_bank(
        db,
        s.ImportInput(
            account_id=card.account_id,
            source_name=data.source_name,
            source_sha256=data.source_sha256,
            rows=[
                s.BankRow(**row.model_dump(exclude={"activity_status"}))
                for row in data.rows
            ],
        ),
        actor,
    )
    for ident in result["observation_ids"]:
        observation = svc.get(db, m.BankObservation, ident)
        observation.source = {
            **observation.source,
            "card_id": card.id,
            "card_alias_id": alias.id,
            "activity_status": "posted",
        }
    return {**result, "card_id": card.id, "financial_entries_added": False}


def _source(db, ref, role, entity_id, expected_account=None):
    from app.portfolio.observation_state import observation_state
    from app.portfolio.reports import ledger

    svc = _svc()
    obs = svc.get(db, m.BankObservation, ref.observation_id)
    account = svc.scope(db, obs.account_id, entity_id)
    if account.role != role or (
        expected_account and obs.account_id != expected_account
    ):
        svc.fail(
            "card_source_account",
            "Source observation belongs to the wrong bank, card or company",
        )
    if (
        obs.status not in {"imported", "posted"}
        or (obs.source or {}).get("activity_status", "posted") != "posted"
        or any(
            str((obs.source or {}).get(key, "")).casefold() in {"pending", "declined"}
            for key in ("status", "transaction_status")
        )
    ):
        svc.fail(
            "card_activity_status",
            "Use a distinct posted issuer or bank movement, not a pending, declined or duplicate row",
        )
    if (
        db.query(m.CardActivity)
        .filter(
            (m.CardActivity.observation_id == obs.id)
            | (m.CardActivity.bank_observation_id == obs.id)
        )
        .first()
    ):
        svc.fail(
            "card_source_used",
            "This source movement already belongs to a typed card activity",
        )
    state = observation_state(db, obs.id)
    if state["state_digest"] != ref.expected_state_digest:
        svc.fail("stale_state", "Refresh the source observation state")
    rows = ledger(db, obs.date, event_ids=state["event_ids"])
    physical = [r for r in rows if r["role"] in {"bank", "credit_card"}]
    residuals = state["suspense_residuals"]["accrual"]
    reviews = []
    if obs.event_id:
        expected = {
            (obs.event_id, obs.account_id, obs.date.isoformat()): obs.amount_cents
        }
        for basis in ("accrual", "modified_cash_bank_posting"):
            movement = defaultdict(int)
            for row in ledger(db, obs.date, basis=basis, event_ids=state["event_ids"]):
                if row["role"] in {"bank", "credit_card"}:
                    movement[(row["event_id"], row["account_id"], row["date"])] += (
                        row["debit_cents"] - row["credit_cents"]
                    )
            if {k: v for k, v in movement.items() if v} != expected:
                svc.fail(
                    "card_source_lineage",
                    "Source liability or cash must match the exact original observation on both bases",
                )
        if (
            residuals != state["suspense_residuals"]["modified_cash_bank_posting"]
            or sum(r["amount_cents"] for r in residuals) != -obs.amount_cents
            or any(r["entity_id"] != entity_id for r in residuals)
            or any(
                svc.get(db, m.Event, ident).date > obs.date
                for ident in state["event_ids"]
            )
            or len({r["line_id"] for r in physical}) != 1
        ):
            svc.fail(
                "card_source_residual",
                "Classify only a complete unchanged source suspense balance on its actual date",
            )
        reviews = (
            db.query(m.ReviewItem)
            .filter_by(observation_id=obs.id, code="suspense", status="open")
            .all()
        )
        if not reviews:
            svc.fail(
                "review_state",
                "Posted card activity needs its open source suspense review",
            )
    elif state["event_ids"]:
        svc.fail(
            "card_source_lineage",
            "Unposted observation unexpectedly has an existing correction family",
        )
    return {
        "obs": obs,
        "account": account,
        "state": state,
        "residuals": residuals,
        "physical": physical,
        "reviews": reviews,
    }


def _source_lines(source, components=None, bank_fund_id=None, item=None):
    svc = _svc()
    obs = source["obs"]
    if obs.event_id:
        return [
            svc.line(
                r["account_id"], -r["amount_cents"], **{k: r.get(k) for k in DIMENSIONS}
            )
            for r in source["residuals"]
        ]
    if components:
        direction = 1 if obs.amount_cents > 0 else -1
        return [
            svc.line(
                obs.account_id,
                direction * c["amount_cents"],
                property_id=c.get("property_id"),
                unit_id=c.get("unit_id"),
                item=c["item"],
            )
            for c in components
        ]
    return [
        svc.line(
            obs.account_id,
            obs.amount_cents,
            fund_id=bank_fund_id,
            item=item,
            cash_flow="financing" if source["account"].role == "bank" else None,
        )
    ]


def _finish(db, source, result, evidence, components=None, bank_flow=False):
    from app.portfolio.observation_state import observation_state
    from app.portfolio.reports import ledger

    svc = _svc()
    obs = source["obs"]
    if not obs.event_id:
        obs.event_id = result["event_id"]
        obs.status = "posted"
    else:
        # Effective property/component attribution preserves the original issuer
        # liability line and its dated signed amount; no replacement liability.
        if components:
            physical_id = source["physical"][0]["line_id"]
            prior = (
                db.query(m.LineAllocation)
                .filter_by(line_id=physical_id)
                .order_by(m.LineAllocation.date.desc(), m.LineAllocation.id.desc())
                .first()
            )
            direction = 1 if obs.amount_cents > 0 else -1
            db.add(
                m.LineAllocation(
                    line_id=physical_id,
                    event_id=result["event_id"],
                    date=obs.date,
                    components=[
                        {
                            "amount_cents": direction * c["amount_cents"],
                            "property_id": c.get("property_id"),
                            "unit_id": c.get("unit_id"),
                            "fund_id": None,
                            "counterparty_id": None,
                            "item": c["item"],
                            "cash_flow": None,
                        }
                        for c in components
                    ],
                    evidence=evidence,
                    supersedes_id=prior.id if prior else None,
                )
            )
        if bank_flow:
            for ident in {r["line_id"] for r in source["physical"]}:
                db.add(
                    m.CashClassification(
                        line_id=ident,
                        event_id=result["event_id"],
                        date=obs.date,
                        cash_flow="financing",
                        reason=evidence,
                    )
                )
    db.flush()
    after = observation_state(db, obs.id)
    for basis in ("accrual", "modified_cash_bank_posting"):
        actual = defaultdict(int)
        for row in ledger(db, obs.date, basis=basis, event_ids=after["event_ids"]):
            if row["role"] in {"bank", "credit_card"}:
                actual[(row["event_id"], row["account_id"], row["date"])] += (
                    row["debit_cents"] - row["credit_cents"]
                )
        expected = {
            (obs.event_id, obs.account_id, obs.date.isoformat()): obs.amount_cents
        }
        if {key: value for key, value in actual.items() if value} != expected:
            svc.fail(
                "card_source_preservation",
                "Typed accounting changed the original dated source movement",
            )
    if any(after["suspense_residuals"].values()):
        svc.fail(
            "card_source_residual",
            "The typed card operation did not clear source suspense on both bases",
        )
    for review in source["reviews"]:
        review.status = "resolved"
        review.resolution = evidence
        review.resolved_event_id = result["event_id"]
    return [r.id for r in source["reviews"]]


def _cost_lines(db, card, components, *, refund=False):
    svc = _svc()
    sign = -1 if refund else 1
    journals = defaultdict(list)
    management = []
    for component in components:
        amount = sign * component["amount_cents"]
        kind = component["beneficiary_kind"]
        target_entity = component["entity_id"]
        dims = {k: component.get(k) for k in ("property_id", "unit_id", "item")}
        if kind == "external_party":
            journals[card.entity_id].append(
                svc.line(svc.control(db, card.entity_id, "receivable"), amount, **dims)
            )
        else:
            target = svc.scope(db, component["account_id"], target_entity)
            if target.role not in {"expense", "cogs", "asset", "prepaid"}:
                svc.fail(
                    "card_cost_account",
                    "Choose an ordinary cost or asset account; protected settlement controls need their own workflow",
                )
            journals[target_entity].append(
                svc.line(component["account_id"], amount, **dims)
            )
            if target.role in {"expense", "cogs"}:
                management.append(
                    s.ManagementInput(
                        entity_id=target_entity,
                        property_id=component.get("property_id"),
                        account_id=component["account_id"],
                        date=component["date"],
                        amount_cents=-amount,
                    )
                )
            if kind == "entity":
                journals[card.entity_id].append(
                    svc.line(
                        svc.control(db, card.entity_id, "due_from"),
                        amount,
                        counterparty_id=target_entity,
                        **dims,
                    )
                )
                journals[target_entity].append(
                    svc.line(
                        svc.control(db, target_entity, "due_to"),
                        -amount,
                        counterparty_id=card.entity_id,
                        **dims,
                    )
                )
    return journals, management


def _unique_code(db, code):
    if db.query(m.CardActivity).filter_by(code=code).first():
        _svc().fail(
            "card_activity_identity",
            "This stable card activity code is already recorded",
        )


def classify_card_purchase(db, data, actor):
    _unique_code(db, data.code)
    svc = _svc()
    card = _identity(db, data.card_id)
    source = _source(
        db, data.observation, "credit_card", card.entity_id, card.account_id
    )
    obs = source["obs"]
    if (
        obs.amount_cents >= 0
        or sum(c.amount_cents for c in data.components) != -obs.amount_cents
    ):
        svc.fail(
            "card_purchase_amount",
            "Allocate the exact posted issuer purchase liability once",
        )
    if len({c.key for c in data.components}) != len(data.components):
        svc.fail("card_components", "Component keys must be unique")
    components = []
    for part in data.components:
        component = part.model_dump(mode="json")
        if part.beneficiary_kind == "company":
            if part.entity_id not in (None, card.entity_id):
                svc.fail(
                    "card_beneficiary", "Company costs belong to the card's company"
                )
            component["entity_id"] = card.entity_id
        elif part.beneficiary_kind == "entity":
            svc.get(db, m.Entity, part.entity_id)
            if part.entity_id == card.entity_id:
                svc.fail(
                    "card_beneficiary", "Use company cost for the card's own company"
                )
        else:
            party = svc.get(db, m.Party, part.party_id)
            if party.entity_id:
                svc.fail(
                    "card_beneficiary",
                    "A maintained beneficiary requires reciprocal entity accounting",
                )
            component["entity_id"] = card.entity_id
        component["item"] = f"card:{data.code}:{part.key}"
        component["date"] = obs.date.isoformat()
        components.append(component)
    journals, management = _cost_lines(db, card, components)
    journals[card.entity_id] = (
        _source_lines(source, components) + journals[card.entity_id]
    )
    result = svc.post(
        db,
        s.Posting(
            date=obs.date,
            description="Card purchase " + data.code,
            rationale=data.evidence,
            kind="card_purchase",
            corrects_id=obs.event_id,
            source={
                "observation_id": obs.id,
                "card_id": card.id,
                "components": components,
            },
            journals=[
                s.Journal(entity_id=entity, lines=lines)
                for entity, lines in journals.items()
            ],
            management=management,
            observed=True,
        ),
        actor,
    )
    for component in components:
        if component["beneficiary_kind"] == "external_party":
            item = m.OpenItem(
                code=component["item"],
                entity_id=card.entity_id,
                property_id=component.get("property_id"),
                event_id=result["event_id"],
                kind="party_receivable",
                account_id=svc.control(db, card.entity_id, "receivable"),
                control_id=svc.control(db, card.entity_id, "receivable"),
                amount_cents=component["amount_cents"],
                date=obs.date,
            )
            db.add(item)
            db.flush()
            component["item_id"] = item.id
    closed = _finish(db, source, result, data.evidence, components)
    activity = m.CardActivity(
        code=data.code,
        card_id=card.id,
        kind="purchase",
        observation_id=obs.id,
        event_id=result["event_id"],
        item="card:" + data.code,
        amount_cents=-obs.amount_cents,
        components=components,
        evidence=data.evidence,
        actor=actor,
    )
    db.add(activity)
    db.flush()
    return {
        **result,
        "card_activity_id": activity.id,
        "resolved_review_ids": closed,
        "source_kind": "card_liability",
        "bank_cash_delta_cents": 0,
        "cost_cents": activity.amount_cents,
        "original_liability_preserved": bool(source["physical"]),
    }


def _reciprocal_refund_headroom(db, card, component, refund_date, amount):
    """A backdated refund must leave both real counterparties payable later too."""
    from app.portfolio.reports import ledger

    svc = _svc()
    beneficiary = component["entity_id"]
    scopes = (
        (card.entity_id, "due_from", beneficiary, 1),
        (beneficiary, "due_to", card.entity_id, -1),
    )
    for entity_id, role, counterparty_id, sign in scopes:
        account_id = svc.control(db, entity_id, role)
        for basis in ("accrual", "modified_cash_bank_posting"):
            changes = defaultdict(int)
            for row in ledger(db, date.max, account_id=account_id, basis=basis):
                if row.get("item") != component["item"]:
                    continue
                if any(
                    row.get(key) != expected
                    for key, expected in (
                        ("counterparty_id", counterparty_id),
                        ("property_id", component.get("property_id")),
                        ("unit_id", component.get("unit_id")),
                    )
                ):
                    svc.fail(
                        "card_refund_settled",
                        "The original reciprocal component has changed scope; establish its refund obligation separately",
                    )
                changes[row["date"]] += sign * (
                    row["debit_cents"] - row["credit_cents"]
                )
            # Include the refund date even when it has no existing posting.
            changes[refund_date.isoformat()] += 0
            running = 0
            for day, movement in sorted(changes.items()):
                running += movement
                if day >= refund_date.isoformat() and running < amount:
                    svc.fail(
                        "card_refund_settled",
                        "Refund exceeds this beneficiary's remaining reciprocal charge on its date or a later recorded date",
                    )


def record_card_refund(db, data, actor):
    _unique_code(db, data.code)
    svc = _svc()
    card = _identity(db, data.card_id)
    purchase = svc.get(db, m.CardActivity, data.purchase_id)
    if purchase.kind != "purchase" or purchase.card_id != card.id:
        svc.fail(
            "card_refund_parent", "Refund must identify a purchase on this exact card"
        )
    source = _source(
        db, data.observation, "credit_card", card.entity_id, card.account_id
    )
    obs = source["obs"]
    if (
        obs.date < svc.get(db, m.BankObservation, purchase.observation_id).date
        or obs.amount_cents <= 0
        or sum(c.amount_cents for c in data.components) != obs.amount_cents
    ):
        svc.fail(
            "card_refund_amount",
            "Refund must follow the purchase and equal the actual issuer credit",
        )
    originals = {c["key"]: c for c in purchase.components}
    if len({c.key for c in data.components}) != len(data.components):
        svc.fail("card_components", "Refund component keys must be unique")
    refunded = defaultdict(int)
    for prior in db.query(m.CardActivity).filter_by(
        parent_id=purchase.id, kind="refund"
    ):
        for component in prior.components:
            refunded[component["key"]] += component["amount_cents"]
    components = []
    for part in data.components:
        original = originals.get(part.key)
        if (
            not original
            or part.amount_cents + refunded[part.key] > original["amount_cents"]
        ):
            svc.fail(
                "card_refund_limit",
                "Refund exceeds the unrefunded original cost component",
            )
        component = {
            **original,
            "amount_cents": part.amount_cents,
            "date": obs.date.isoformat(),
        }
        if component["beneficiary_kind"] == "external_party":
            item = svc.get(db, m.OpenItem, component["item_id"])
            applied = sum(
                a.amount_cents
                for a in db.query(m.ItemApplication).filter_by(item_id=item.id)
            )
            if item.amount_cents - applied < part.amount_cents:
                svc.fail(
                    "card_refund_settled",
                    "The beneficiary already settled this amount; establish the resulting refund obligation separately",
                )
        elif component["beneficiary_kind"] == "entity":
            _reciprocal_refund_headroom(
                db, card, component, obs.date, part.amount_cents
            )
        components.append(component)
    journals, management = _cost_lines(db, card, components, refund=True)
    journals[card.entity_id] = (
        _source_lines(source, components) + journals[card.entity_id]
    )
    result = svc.post(
        db,
        s.Posting(
            date=obs.date,
            description="Card refund " + data.code,
            rationale=data.evidence,
            kind="card_refund",
            corrects_id=obs.event_id,
            source={
                "observation_id": obs.id,
                "card_id": card.id,
                "purchase_activity_id": purchase.id,
                "components": components,
            },
            journals=[
                s.Journal(entity_id=entity, lines=lines)
                for entity, lines in journals.items()
            ],
            management=management,
            observed=True,
        ),
        actor,
    )
    for component in components:
        if component["beneficiary_kind"] == "external_party":
            db.add(
                m.ItemApplication(
                    item_id=component["item_id"],
                    event_id=result["event_id"],
                    amount_cents=component["amount_cents"],
                )
            )
    closed = _finish(db, source, result, data.evidence, components)
    activity = m.CardActivity(
        code=data.code,
        card_id=card.id,
        kind="refund",
        observation_id=obs.id,
        event_id=result["event_id"],
        parent_id=purchase.id,
        item=purchase.item,
        amount_cents=obs.amount_cents,
        components=components,
        evidence=data.evidence,
        actor=actor,
    )
    db.add(activity)
    db.flush()
    return {
        **result,
        "card_activity_id": activity.id,
        "resolved_review_ids": closed,
        "bank_cash_delta_cents": 0,
        "refunded_cents": obs.amount_cents,
    }


def _bank_fund(db, source, requested):
    svc = _svc()
    original = {r["fund_id"] for r in source["physical"]}
    if source["obs"].event_id:
        if len(original) != 1 or requested != next(iter(original)):
            svc.fail(
                "card_payment_fund",
                "Preserve the bank source's exact original fund attribution",
            )
    if source["account"].pooled and not requested:
        svc.fail(
            "card_payment_fund",
            "A pooled bank payment needs its actual own-company source fund",
        )
    if requested:
        fund = svc.get(db, m.Fund, requested)
        if (
            fund.holder_entity_id != source["account"].entity_id
            or svc.get(db, m.Party, fund.beneficiary_id).entity_id
            != fund.holder_entity_id
            or fund.restriction != "unrestricted"
            or fund.purpose == "unresolved"
        ):
            svc.fail(
                "card_payment_fund",
                "Card repayment cannot consume another beneficiary's or unidentified money",
            )


def _payment(db, data, actor, returned=False):
    _unique_code(db, data.code)
    svc = _svc()
    card = _identity(db, data.card_id)
    bank = _source(db, data.bank_observation, "bank", card.entity_id)
    issuer = _source(
        db, data.issuer_observation, "credit_card", card.entity_id, card.account_id
    )
    bank_obs, card_obs = bank["obs"], issuer["obs"]
    direction = -1 if returned else 1
    if (
        bank_obs.amount_cents != -card_obs.amount_cents
        or card_obs.amount_cents * direction <= 0
    ):
        svc.fail(
            "card_payment_amount",
            "Pair the exact opposite actual bank and issuer movements; keep fees separate",
        )
    _bank_fund(db, bank, data.bank_fund_id)
    amount = abs(card_obs.amount_cents)
    parent = None
    retry = None
    if returned:
        if data.retry_of_return_id:
            svc.fail("card_return", "A return cannot simultaneously be a retry")
        parent = svc.get(db, m.CardActivity, data.original_payment_id)
        if (
            parent.kind != "repayment"
            or parent.card_id != card.id
            or parent.item != data.item
        ):
            svc.fail(
                "card_return", "Identify the exact original card repayment and its item"
            )
        original_bank = svc.get(db, m.BankObservation, parent.bank_observation_id)
        original_issuer = svc.get(db, m.BankObservation, parent.observation_id)
        if (
            bank_obs.account_id != original_bank.account_id
            or bank_obs.date < original_bank.date
            or card_obs.date < original_issuer.date
        ):
            svc.fail(
                "card_return",
                "Return legs must follow their own original settlement dates and bank account",
            )
        returned_amount = sum(
            row.amount_cents
            for row in db.query(m.CardActivity).filter_by(
                parent_id=parent.id, kind="repayment_return"
            )
        )
        if returned_amount + amount > parent.amount_cents:
            svc.fail(
                "card_return_limit",
                "Return exceeds the original payment's unreturned amount",
            )
    else:
        if (
            db.query(m.CardActivity)
            .filter_by(card_id=card.id, kind="repayment", item=data.item)
            .first()
        ):
            svc.fail(
                "card_payment_item",
                "Each actual payment attempt needs a distinct payment item",
            )
        if data.retry_of_return_id:
            retry = svc.get(db, m.CardActivity, data.retry_of_return_id)
            if retry.kind != "repayment_return" or retry.card_id != card.id:
                svc.fail(
                    "card_retry", "Retry must identify a returned payment on this card"
                )
            returned_bank = svc.get(db, m.BankObservation, retry.bank_observation_id)
            returned_issuer = svc.get(db, m.BankObservation, retry.observation_id)
            if (
                bank_obs.account_id != returned_bank.account_id
                or bank_obs.date < returned_bank.date
                or card_obs.date < returned_issuer.date
            ):
                svc.fail(
                    "card_retry",
                    "Retry legs must follow the corresponding actual returns in the same bank",
                )
            retried = sum(
                row.amount_cents
                for row in db.query(m.CardActivity).filter_by(
                    retry_of_id=retry.id, kind="repayment"
                )
            )
            if retried + amount > retry.amount_cents:
                svc.fail(
                    "card_retry_limit",
                    "Retry exceeds the returned amount not already retried",
                )
    bridge = svc.control(db, card.entity_id, "bridge")
    results = []
    closed = []
    for source in (bank, issuer):
        obs = source["obs"]
        entries = _source_lines(
            source,
            bank_fund_id=data.bank_fund_id if source is bank else None,
            item=data.item,
        )
        entries.append(
            svc.line(
                bridge, -obs.amount_cents, item=data.item, fund_id=data.bank_fund_id
            )
        )
        result = svc.post(
            db,
            s.Posting(
                date=obs.date,
                description=("Card payment return " if returned else "Card repayment ")
                + data.item,
                rationale=data.evidence,
                kind="card_payment_return" if returned else "card_repayment",
                corrects_id=obs.event_id,
                observed=True,
                source={
                    "observation_id": obs.id,
                    "paired_observation_id": (
                        card_obs.id if source is bank else bank_obs.id
                    ),
                    "card_id": card.id,
                    "item": data.item,
                    "original_payment_id": parent.id if parent else None,
                    "retry_of_return_id": retry.id if retry else None,
                },
                journals=[s.Journal(entity_id=card.entity_id, lines=entries)],
            ),
            actor,
        )
        closed += _finish(db, source, result, data.evidence, bank_flow=source is bank)
        results.append(result)
    # Transit closes per payment item after both actual dated legs. The books
    # retain the real intermediate debit/credit while either side is in transit.
    if svc.balance(db, bridge, max(bank_obs.date, card_obs.date), item=data.item) != 0:
        svc.fail(
            "card_payment_clearing",
            "The paired settlement must clear exactly without cost or extra cash",
        )
    activity = m.CardActivity(
        code=data.code,
        card_id=card.id,
        kind="repayment_return" if returned else "repayment",
        observation_id=card_obs.id,
        event_id=results[1]["event_id"],
        bank_observation_id=bank_obs.id,
        bank_event_id=results[0]["event_id"],
        parent_id=parent.id if parent else None,
        retry_of_id=retry.id if retry else None,
        item=data.item,
        amount_cents=amount,
        components=[],
        evidence=data.evidence,
        actor=actor,
    )
    db.add(activity)
    db.flush()
    coverage = card_state(db, card.id, max(bank_obs.date, card_obs.date))
    coverage_review_ids = []
    if not coverage["coverage_current"]:
        review = m.ReviewItem(
            code="card_coverage",
            entity_id=card.entity_id,
            event_id=activity.event_id,
            observation_id=card_obs.id,
            detail="Actual bank/card settlement matched; complete issuer opening and statement coverage remain unverified. Payment classification does not certify the issuer balance.",
        )
        db.add(review)
        db.flush()
        coverage_review_ids.append(review.id)
    return {
        "card_activity_id": activity.id,
        "event_ids": [r["event_id"] for r in results],
        "resolved_review_ids": closed,
        "open_coverage_review_ids": coverage_review_ids,
        "bank_date": bank_obs.date.isoformat(),
        "issuer_date": card_obs.date.isoformat(),
        "bank_cash_delta_cents": bank_obs.amount_cents if not bank["physical"] else 0,
        "card_liability_delta_cents": (
            card_obs.amount_cents if not issuer["physical"] else 0
        ),
        "expense_delta_cents": 0,
        "clearing_balance_cents": 0,
        "issuer_balance_certified_by_payment": False,
    }


def match_card_repayment(db, data, actor):
    return _payment(db, data, actor)


def return_card_repayment(db, data, actor):
    return _payment(db, data, actor, returned=True)


def _inventory(db, card, start, end):
    return (
        db.query(m.BankObservation)
        .filter(
            m.BankObservation.account_id == card.account_id,
            m.BankObservation.date >= start,
            m.BankObservation.date <= end,
            m.BankObservation.status != "duplicate",
        )
        .order_by(m.BankObservation.id)
        .all()
    )


def _coverage_valid(db, card, coverage):
    from app.portfolio.reports import ledger

    observations = _inventory(db, card, coverage.start, coverage.end)
    if not (
        all(
            o.event_id
            and o.status == "posted"
            and (o.source or {}).get("activity_status", "posted") == "posted"
            and not any(
                str((o.source or {}).get(key, "")).casefold() in {"pending", "declined"}
                for key in ("status", "transaction_status")
            )
            for o in observations
        )
        and len(coverage.observation_ids) == len(set(coverage.observation_ids))
        and set(coverage.observation_ids) == {o.id for o in observations}
        and coverage.opening_liability_cents - sum(o.amount_cents for o in observations)
        == coverage.ending_liability_cents
    ):
        return False
    expected = defaultdict(int)
    for obs in observations:
        expected[(obs.event_id, obs.date.isoformat())] += obs.amount_cents
    expected = {key: value for key, value in expected.items() if value}
    opening_events = {
        event.id
        for event in db.query(m.Event).filter_by(date=coverage.start, kind="opening")
    }
    for basis in ("accrual", "modified_cash_bank_posting"):
        physical = defaultdict(int)
        opening = ending = 0
        for line in ledger(db, coverage.end, account_id=card.account_id, basis=basis):
            amount = line["debit_cents"] - line["credit_cents"]
            ending += amount
            # Only explicit, physical opening entries at this interval's start
            # belong to its reviewed opening. Later openings are missing activity.
            is_opening = line["event_id"] in opening_events and not line["virtual"]
            if line["date"] < coverage.start.isoformat() or is_opening:
                opening += amount
            else:
                physical[(line["event_id"], line["date"])] += amount
        if (
            {key: value for key, value in physical.items() if value} != expected
            or any(
                physical[(o.event_id, o.date.isoformat())] != o.amount_cents
                for o in observations
            )
            or coverage.opening_liability_cents != -opening
            or coverage.ending_liability_cents != -ending
        ):
            return False
    return True


def record_card_coverage(db, data, actor):
    svc = _svc()
    card = _identity(db, data.card_id)
    state = card_state(db, card.id, data.end)
    if state["state_digest"] != data.expected_state_digest:
        svc.fail(
            "stale_state",
            "Refresh the issuer coverage state before recording the reviewed statement",
        )
    if data.end < data.start or len(data.observation_ids) != len(
        set(data.observation_ids)
    ):
        svc.fail(
            "card_coverage",
            "Provide a valid issuer interval and each observation exactly once",
        )
    retired = {
        r.supersedes_id
        for r in db.query(m.CardCoverage).filter_by(card_id=card.id)
        if r.supersedes_id
    }
    if data.supersedes_id:
        previous = svc.get(db, m.CardCoverage, data.supersedes_id)
        if previous.id in retired or (
            previous.card_id,
            previous.start,
            previous.end,
        ) != (card.id, data.start, data.end):
            svc.fail(
                "card_coverage_revision",
                "Revise the current issuer coverage for this exact card and interval",
            )
    for prior in db.query(m.CardCoverage).filter(
        m.CardCoverage.card_id == card.id,
        m.CardCoverage.start <= data.end,
        m.CardCoverage.end >= data.start,
    ):
        if prior.id not in retired and prior.id != data.supersedes_id:
            svc.fail(
                "card_coverage_overlap",
                "Reviewed issuer intervals cannot overlap or replace earlier evidence implicitly",
            )
    if (
        not db.query(m.DocumentLocation)
        .filter_by(document_id=data.document_id, sha256=data.sha256)
        .first()
    ):
        svc.fail(
            "card_coverage_source",
            "Issuer coverage needs the exact registered statement document hash",
        )
    row = m.CardCoverage(
        **data.model_dump(exclude={"expected_state_digest"}), actor=actor
    )
    if not _coverage_valid(db, card, row):
        svc.fail(
            "card_coverage_difference",
            "Issuer coverage requires complete posted inventory and supported opening/ending liability",
        )
    db.add(row)
    db.flush()
    closed = []
    current = card_state(db, card.id, data.end)
    if current["coverage_current"]:
        for review, obs in (
            db.query(m.ReviewItem, m.BankObservation)
            .join(
                m.BankObservation, m.BankObservation.id == m.ReviewItem.observation_id
            )
            .filter(
                m.ReviewItem.code == "card_coverage",
                m.ReviewItem.status == "open",
                m.BankObservation.account_id == card.account_id,
                m.BankObservation.date
                >= date.fromisoformat(current["supported_start"]),
                m.BankObservation.date <= data.end,
            )
        ):
            review.status = "resolved"
            review.resolution = f"Reviewed issuer coverage {row.id} through {data.end.isoformat()}; source observation {obs.id}. No intraperiod statement cutoff is certified. {data.evidence}"
            closed.append(review.id)
    return {
        "card_coverage_id": row.id,
        "card_id": card.id,
        "supported_start": row.start.isoformat(),
        "supported_end": row.end.isoformat(),
        "resolved_review_ids": closed,
        "financial_entries_added": False,
    }


def card_state(db, card_id, through=None):
    from app.portfolio.reports import ledger, read_snapshot

    svc = _svc()
    read_snapshot(db)
    card = _identity(db, card_id)
    through = through or date.today()
    rows = (
        db.query(m.CardCoverage)
        .filter(m.CardCoverage.card_id == card.id, m.CardCoverage.end <= through)
        .order_by(m.CardCoverage.start, m.CardCoverage.end)
        .all()
    )
    all_versions = (
        db.query(m.CardCoverage)
        .filter_by(card_id=card.id)
        .order_by(m.CardCoverage.id)
        .all()
    )
    retired = {r.supersedes_id for r in all_versions if r.supersedes_id}
    rows = [r for r in rows if r.id not in retired]
    valid = []
    invalid = []
    gaps = []
    supported_end = None
    supported_start = None
    account = svc.scope(db, card.account_id)
    expected_start = account.coverage_start
    for row in rows:
        if not _coverage_valid(db, card, row):
            invalid.append(row.id)
            continue
        if supported_start is None:
            supported_start = row.start
            if expected_start and row.start > expected_start:
                gaps.append(
                    {
                        "start": expected_start.isoformat(),
                        "end": (row.start - timedelta(days=1)).isoformat(),
                    }
                )
        if supported_end and row.start > supported_end + timedelta(days=1):
            gaps.append(
                {
                    "start": (supported_end + timedelta(days=1)).isoformat(),
                    "end": (row.start - timedelta(days=1)).isoformat(),
                }
            )
        supported_end = row.end
        valid.append(row.id)
    result = {
        "card": svc.values(card),
        "as_of": through.isoformat(),
        "aliases": [
            svc.values(a)
            for a in db.query(m.CardAlias)
            .filter_by(card_id=card.id)
            .order_by(m.CardAlias.id)
        ],
        "book_liability_cents": -svc.balance(db, card.account_id, through),
        "coverage_current": bool(
            valid and not gaps and not invalid and supported_end == through
        ),
        "coverage_ids": valid,
        "invalid_coverage_ids": invalid,
        "coverage_gaps": gaps,
        "supported_start": supported_start.isoformat() if supported_start else None,
        "supported_end": supported_end.isoformat() if supported_end else None,
        "coverage_scope": "Only the exact reviewed issuer periods; aliases and payment classification do not establish opening coverage.",
        "source_inventory_digest": svc.digest(
            [
                svc.values(o)
                for o in db.query(m.BankObservation)
                .filter(
                    m.BankObservation.account_id == card.account_id,
                    m.BankObservation.date <= through,
                )
                .order_by(m.BankObservation.id)
            ]
        ),
        "coverage_versions": [
            svc.values(row) for row in all_versions if row.end <= through
        ],
        "source_ledger_digest": svc.digest(
            {
                basis: ledger(db, through, account_id=card.account_id, basis=basis)
                for basis in ("accrual", "modified_cash_bank_posting")
            }
        ),
        "read_only": True,
    }
    result["state_digest"] = svc.digest(result)
    return result


CARD_OPERATIONS = {
    name: globals()[name]
    for name in (
        "register_card_identity",
        "add_card_alias",
        "import_card_activity",
        "classify_card_purchase",
        "record_card_refund",
        "match_card_repayment",
        "return_card_repayment",
        "record_card_coverage",
    )
}
