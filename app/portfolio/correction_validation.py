"""Metadata-only validation of explicit, complete correction families.

Source identities are checked before totals: equal amounts never establish
lineage. Historical decoders recognize specific immutable source contracts;
they neither rewrite source JSON nor attest external evidence on their own.
"""

from collections import defaultdict
from datetime import date
import json

from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS

SOURCE_ROLES = {"bank", "credit_card"}
BASES = ("accrual", "modified_cash_bank_posting")
HISTORICAL_BENEFICIARY_DECODER = "held-owner-distribution/v1"


def _beneficiary(correction, obs):
    from app.portfolio.service import fail

    source = correction.source or {}
    current = source.get("party_id")
    historical = source.get("beneficiary_party_id")
    for value in (current, historical):
        if value is not None and (type(value) is not int or value <= 0):
            fail(
                "wrong_beneficiary", "Beneficiary identity must be a positive party ID"
            )
    if current is not None and historical is not None and current != historical:
        fail(
            "wrong_beneficiary",
            "Current and historical beneficiary identities conflict",
        )
    if historical is None:
        return current, None
    # The pre-versioned historical shape is explicitly decoder v1. Unknown
    # versions and arbitrary journals containing a similarly named field fail.
    if (
        correction.kind != "held_owner_distribution_classification"
        or type(source.get("beneficiary_schema_version", 1)) is not int
        or source.get("beneficiary_schema_version", 1) != 1
        or type(source.get("observation_id")) is not int
        or source["observation_id"] != obs.id
        or not isinstance(source.get("item"), str)
        or not source["item"].strip()
        or not isinstance(source.get("exact_bank_pair_proof"), dict)
        or not source["exact_bank_pair_proof"]
        or not isinstance(source.get("direct_user_confirmation"), str)
        or not source["direct_user_confirmation"].strip()
        or type(source.get("initial_distribution_cents")) is not int
        or source["initial_distribution_cents"] != obs.amount_cents
        or obs.amount_cents <= 0
    ):
        fail(
            "unsupported_beneficiary_source",
            "Historical beneficiary source is not a recognized v1 correction",
        )
    return historical, HISTORICAL_BENEFICIARY_DECODER


def _movements(db, obs, state, manifest, rows_by_basis):
    from app.portfolio.service import fail, get, scope

    if manifest is None:
        declared = [
            {
                "observation_id": obs.id,
                "account_id": obs.account_id,
                "date": obs.date.isoformat(),
                "amount_cents": obs.amount_cents,
            }
        ]
        kind = "single_bank"
    else:
        declared = [row.model_dump(mode="json") for row in manifest.observations]
        kind = manifest.kind
    ids = [row["observation_id"] for row in declared]
    if len(set(ids)) != len(ids) or obs.id not in ids:
        fail(
            "movement_manifest",
            "Declare each observation once, including the reviewed observation",
        )
    if len(ids) != (2 if kind == "paired_bank" else 1):
        fail(
            "movement_manifest", "Movement family has the wrong number of observations"
        )
    role = "credit_card" if kind == "card_liability" else "bank"
    expected = defaultdict(int)
    original_events = set()
    for declaration in declared:
        original = get(db, m.BankObservation, declaration["observation_id"])
        actual = {
            "observation_id": original.id,
            "account_id": original.account_id,
            "date": original.date.isoformat(),
            "amount_cents": original.amount_cents,
        }
        if declaration != actual or not original.amount_cents:
            fail(
                "movement_identity",
                "Declared movement differs from the immutable observation",
            )
        if scope(db, original.account_id).role != role:
            fail(
                "movement_manifest",
                "Observation account does not match the declared movement family",
            )
        if not original.event_id or original.event_id not in state["event_ids"]:
            fail(
                "correction_lineage",
                "Every declared observation must already belong to the explicit correction family",
            )
        original_events.add(original.event_id)
        expected[
            (original.event_id, original.account_id, original.date.isoformat())
        ] += original.amount_cents
    # Do not let offsetting observations disappear in an account/date net sum.
    linked_observations = {
        row.id
        for row in db.query(m.BankObservation).filter(
            m.BankObservation.event_id.in_(state["event_ids"])
        )
    }
    if linked_observations != set(ids):
        fail(
            "movement_manifest",
            "Declare all and only the original observations in this correction family",
        )
    for rows in rows_by_basis.values():
        originals = defaultdict(int)
        overlays = defaultdict(int)
        for row in rows:
            if row["role"] not in SOURCE_ROLES:
                continue
            key = (row["event_id"], row["account_id"], row["date"])
            amount = row["debit_cents"] - row["credit_cents"]
            if row["event_id"] in original_events:
                originals[key] += amount
            else:
                overlays[key] += amount
        if dict(originals) != dict(expected) or any(overlays.values()):
            fail(
                "cash_lineage",
                "Source movements must match each original observation; corrections must preserve each account and date",
            )
    return {"kind": kind, "observations": declared}, original_events


def validate_existing(db, data, actor):
    from app.portfolio.service import fail, get, line, scope, validate_lines
    from app.portfolio.observation_state import observation_state
    from app.portfolio.reports import ledger

    obs = get(db, m.BankObservation, data.observation.observation_id)
    review = get(db, m.ReviewItem, data.observation.review_id)
    if (
        review.observation_id != obs.id
        or review.code != "suspense"
        or review.status != "open"
    ):
        fail("review_state", "Choose this observation's open suspense review")
    state = observation_state(db, obs.id)
    if state["state_digest"] != data.observation.expected_state_digest:
        fail("stale_state", "Observation changed; inspect it again")
    if (
        data.corrective_event_id == obs.event_id
        or data.corrective_event_id not in state["event_ids"]
    ):
        fail(
            "correction_lineage",
            "Identify a correction explicitly linked to this observation",
        )
    if any(state["suspense_residuals"].values()):
        fail(
            "correction_residual",
            "Scoped suspense remains on one or both reporting bases",
        )
    account = scope(db, obs.account_id)
    correction = get(db, m.Event, data.corrective_event_id)
    if (
        not db.query(m.JournalScope)
        .filter_by(event_id=correction.id, entity_id=account.entity_id)
        .first()
    ):
        fail(
            "wrong_entity",
            "Correction does not belong to the observed source account entity",
        )
    rows_by_basis = {
        basis: ledger(db, date.max, basis=basis, event_ids=state["event_ids"])
        for basis in BASES
    }
    manifest, original_events = _movements(
        db, obs, state, data.movement_manifest, rows_by_basis
    )
    if correction.id in original_events:
        fail(
            "correction_lineage",
            "An original movement cannot be its family's corrective event",
        )
    beneficiary, decoder = _beneficiary(correction, obs)
    if beneficiary is not None:
        get(db, m.Party, beneficiary)
    # Existing one-bank callers never declared cash_flow. Preserve that
    # original matching contract unless the caller opts into an expanded scope.
    complete_scope = data.movement_manifest is not None or any(
        row.model_fields_set & {"entity_id", "cash_flow"}
        or scope(db, row.account_id).entity_id != account.entity_id
        for row in data.balances
    )
    dimensions = (
        DIMENSIONS
        if complete_scope
        else tuple(key for key in DIMENSIONS if key != "cash_flow")
    )
    expected_totals = {}
    reciprocal = defaultdict(int)
    reciprocal_roles = defaultdict(set)
    for expected in data.balances:
        account = scope(db, expected.account_id, expected.entity_id)
        if (
            account.role
            in SOURCE_ROLES | {"bridge", "suspense_credit", "suspense_debit"}
            or expected.amount_cents == 0
        ):
            fail(
                "expected_balance",
                "Identify the nonzero supported economic classification",
            )
        identity = (
            expected.account_id,
            *(getattr(expected, key) for key in dimensions),
        )
        if identity in expected_totals:
            fail("expected_balance", "Each expected scope must be unique")
        expected_totals[identity] = expected.amount_cents
        # Reuse posting's dimension/holder checks, without creating a journal.
        validate_lines(
            db,
            account.entity_id,
            [
                line(
                    expected.account_id,
                    expected.amount_cents,
                    **{key: getattr(expected, key) for key in DIMENSIONS},
                )
            ],
            balanced=False,
        )
        obligation = account.role in {
            "receivable",
            "payable",
            "custody_asset",
            "custody_liability",
        }
        if obligation:
            if not expected.party_id:
                fail(
                    "missing_beneficiary",
                    "Identify the supported obligation beneficiary",
                )
            get(db, m.Party, expected.party_id)
            if expected.fund_id:
                if (
                    get(db, m.Fund, expected.fund_id).beneficiary_id
                    != expected.party_id
                ):
                    fail("wrong_beneficiary", "Fund and specified beneficiary disagree")
            elif beneficiary != expected.party_id:
                fail(
                    "missing_beneficiary",
                    "Correction has no typed association to this party; retain the review",
                )
            if decoder and (
                expected.party_id != beneficiary
                or expected.item != correction.source["item"]
            ):
                fail(
                    "wrong_beneficiary",
                    "Historical beneficiary and individual obligation item must agree",
                )
        if account.role in {"due_from", "due_to"}:
            if (
                not expected.counterparty_id
                or not expected.item
                or expected.counterparty_id == account.entity_id
            ):
                fail(
                    "intercompany_item",
                    "Due From/To needs another entity and the exact individual item",
                )
            get(db, m.Entity, expected.counterparty_id)
            pair = (
                (account.entity_id, expected.counterparty_id)
                if account.role == "due_from"
                else (expected.counterparty_id, account.entity_id)
            )
            key = (*pair, expected.item)
            reciprocal[key] += expected.amount_cents
            reciprocal_roles[key].add(account.role)
        for rows in rows_by_basis.values():
            matching = [
                row
                for row in rows
                if row["account_id"] == expected.account_id
                and all(row.get(key) == getattr(expected, key) for key in dimensions)
            ]
            if sum(
                row["debit_cents"] - row["credit_cents"] for row in matching
            ) != expected.amount_cents or not any(
                (
                    row["event_id"] == correction.id
                    if obligation
                    else row["event_id"] not in original_events
                )
                for row in matching
            ):
                fail(
                    "expected_balance",
                    "Linked correction does not establish the stated scope and amount on both bases",
                )
    if any(reciprocal.values()) or any(
        roles != {"due_from", "due_to"} for roles in reciprocal_roles.values()
    ):
        fail(
            "intercompany_mismatch",
            "Expected Due From/To must be reciprocal for the exact entities and individual item",
        )
    for rows in rows_by_basis.values():
        actual = defaultdict(int)
        entity_totals = defaultdict(int)
        for row in rows:
            amount = row["debit_cents"] - row["credit_cents"]
            if scope(db, row["account_id"]).entity_id != row["entity_id"]:
                fail("wrong_entity", "Effective line belongs to another account entity")
            entity_totals[row["entity_id"]] += amount
            if row["role"] in SOURCE_ROLES or row["role"].startswith("suspense"):
                continue
            actual[(row["account_id"], *(row.get(key) for key in dimensions))] += amount
        if any(entity_totals.values()):
            fail(
                "unbalanced",
                "Correction family must balance separately for every entity on both bases",
            )
        if {key: amount for key, amount in actual.items() if amount} != expected_totals:
            fail(
                "expected_balance",
                "Expected balances must describe the entire effective economic classification",
            )
    review.status = "resolved"
    review.resolved_event_id = correction.id
    review.resolution = json.dumps(
        {
            "method": "validated_existing_correction",
            "evidence": data.evidence,
            "state_digest": state["state_digest"],
            "balances": [row.model_dump() for row in data.balances],
            "movement_manifest": manifest,
            "beneficiary_decoder": decoder,
            "expected_dimensions": list(dimensions),
        },
        sort_keys=True,
    )
    return {
        "review_id": review.id,
        "resolved_review_ids": [review.id],
        "validated_event_id": correction.id,
        "new_financial_events": 0,
        "bank_cash_delta_cents": 0,
        "source_movement_delta_cents": 0,
        "movement_manifest": manifest,
        "beneficiary_decoder": decoder,
        "expected_dimensions": list(dimensions),
    }
