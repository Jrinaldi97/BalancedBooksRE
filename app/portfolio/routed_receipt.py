"""Validate a processor receipt forwarded in full through an intermediary bank."""

from collections import defaultdict
from datetime import date

from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS
from app.portfolio.observation_state import observation_states
from app.portfolio.ledger_snapshot import LedgerSnapshot


def routed_family(db, reviewed, state, manifest):
    from app.portfolio.service import Problem, fail, get, scope

    declared = [r.model_dump(mode="json") for r in manifest.observations]
    ids = [r["observation_id"] for r in declared]
    if len(set(ids)) != 3 or reviewed.id not in ids:
        fail(
            "movement_manifest",
            "Declare the receipt, forwarding withdrawal and beneficiary deposit in order",
        )
    states = observation_states(db, ids)
    receipt, withdrawal, deposit = [s["observation"] for s in states]
    for row, declaration in zip((receipt, withdrawal, deposit), declared):
        if (
            any(
                row[k] != declaration[k] for k in ("account_id", "date", "amount_cents")
            )
            or row["status"] != "posted"
            or scope(db, row["account_id"]).role != "bank"
        ):
            fail(
                "movement_identity",
                "Each leg must match the posted bank observation exactly",
            )
    if not (
        receipt["amount_cents"] > 0
        and receipt["amount_cents"]
        == deposit["amount_cents"]
        == -withdrawal["amount_cents"]
        and receipt["account_id"] == withdrawal["account_id"] != deposit["account_id"]
        and receipt["date"] <= withdrawal["date"] <= deposit["date"]
    ):
        fail(
            "movement_manifest",
            "The receipt must fund the complete onward transfer on the actual bank dates",
        )
    events = sorted(set().union(*(s["event_ids"] for s in states)))
    sources = {e.id: e for e in db.query(m.Event).filter(m.Event.id.in_(events))}
    # Require structured associations between all legs. A coincidental amount,
    # narrative match, or an extra source somewhere in the union is insufficient.
    connected = {ids[0]}
    edges = []
    for e in sources.values():
        src = e.source or {}
        members = {
            v
            for v in [
                src.get("observation_id"),
                src.get("paired_observation_id"),
                *src.get("related_observation_ids", []),
                *src.get("observation_ids", []),
            ]
            if type(v) is int
        }
        if members - set(ids):
            fail(
                "movement_manifest",
                "The source family references an undeclared bank leg",
            )
        edges.append(members)
    for _ in ids:
        for edge in edges:
            if edge & connected:
                connected.update(edge)
    if connected != set(ids) or {
        r.id
        for r in db.query(m.BankObservation).filter(
            m.BankObservation.event_id.in_(events)
        )
    } != set(ids):
        fail(
            "correction_lineage",
            "The three originals must form one explicitly linked receipt and transfer",
        )
    projection = LedgerSnapshot(db, date.max, event_ids=events).project_bases(
        date.max, event_ids=events
    )
    original_events = set()
    residuals = {}
    for basis, rows in projection.items():
        by_event = defaultdict(int)
        basis_originals = set()
        for r in rows:
            if r["role"] in ("bank", "credit_card"):
                by_event[(r["event_id"], r["account_id"], r["date"])] += (
                    r["debit_cents"] - r["credit_cents"]
                )
        movements = {k: v for k, v in by_event.items() if v}
        for leg in declared:
            matched = [
                k
                for k, v in movements.items()
                if sources[k[0]].source.get("observation_id") == leg["observation_id"]
                and k[1:] == (leg["account_id"], leg["date"])
                and v == leg["amount_cents"]
            ]
            if len(matched) != 1:
                fail(
                    "cash_lineage",
                    "Each bank leg requires one identifiable physical journal movement",
                )
            basis_originals.add(matched[0][0])
            del movements[matched[0]]
        if original_events and original_events != basis_originals:
            fail(
                "cash_lineage",
                "The same original bank journals must support both bases",
            )
        original_events = basis_originals
        if movements:
            fail(
                "cash_lineage",
                "Additional cash or card movements exist in the declared family",
            )
        # A legacy forwarding journal tags its own suspense offset with the
        # transfer item and cash-flow category. Permit only that exact remap.
        transfer = get(db, m.Event, withdrawal["event_id"])
        tags = {
            (r["item"], r["cash_flow"])
            for r in rows
            if r["event_id"] == transfer.id
            and r["role"] == "suspense_debit"
            and r["item"]
        }
        legacy = (
            transfer.kind == "transfer"
            and transfer.source.get("paired_observation_id") == deposit["id"]
        )
        grouped = defaultdict(int)
        for r in rows:
            if not r["role"].startswith("suspense"):
                continue
            dims = {k: r.get(k) for k in DIMENSIONS}
            if (
                legacy
                and r["event_id"] in states[1]["event_ids"]
                and r["role"] == "suspense_debit"
                and (dims["item"], dims["cash_flow"]) in tags
            ):
                dims.update(item=None, cash_flow=None)
            grouped[(r["entity_id"], r["account_id"], *dims.values())] += (
                r["debit_cents"] - r["credit_cents"]
            )
        remaining = [
            dict(
                entity_id=k[0],
                account_id=k[1],
                **dict(zip(DIMENSIONS, k[2:])),
                amount_cents=v
            )
            for k, v in grouped.items()
            if v
        ]
        residuals[basis] = remaining
        if remaining:
            raise Problem(
                "correction_residual",
                "Scoped suspense remains in the routed receipt",
                {"basis": basis, "residuals": remaining},
            )
    proof = {
        "kind": manifest.kind,
        "observations": declared,
        "state_digests": {str(i): s["state_digest"] for i, s in zip(ids, states)},
        "individual_residuals": {
            str(i): s["suspense_residuals"] for i, s in zip(ids, states)
        },
        "group_residuals": residuals,
    }
    return {**state, "event_ids": events}, proof, original_events, projection
