"""Evidence requests keep source identity, coverage and obligation roles distinct."""

from collections import defaultdict
from datetime import date, timedelta
import hashlib
import os
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.portfolio import models as m
from app.portfolio.allocations import DIMENSIONS
from app.portfolio.reports import read_snapshot, ledger
from app.portfolio.service import fail, get, scope, values, digest


def local_source(locator, verify=False):
    """Inspect only a registered local regular file; never fetch arbitrary URLs."""
    parsed = urlsplit(locator)
    if parsed.scheme not in ("", "file") or parsed.netloc not in ("", "localhost"):
        return {"status": "remote_not_fetched", "bytes_verified": False}
    path = Path(unquote(parsed.path) if parsed.scheme else locator)
    if not path.is_absolute():
        return {"status": "relative_path_unverified", "bytes_verified": False}
    descriptor = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return {"status": "not_regular_file", "bytes_verified": False}
        if not verify:
            return {
                "status": "available_unverified",
                "size_bytes": before.st_size,
                "bytes_verified": False,
            }
        if before.st_size > 128 * 1024 * 1024:
            return {
                "status": "verification_size_limit",
                "size_bytes": before.st_size,
                "bytes_verified": False,
            }
        hashed, size = hashlib.sha256(), 0
        while block := os.read(descriptor, 1024 * 1024):
            size += len(block)
            if size > 128 * 1024 * 1024:
                return {"status": "verification_size_limit", "bytes_verified": False}
            hashed.update(block)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return {"status": "changed_during_verification", "bytes_verified": False}
        return {
            "status": "hashed",
            "size_bytes": size,
            "sha256": hashed.hexdigest(),
            "bytes_verified": True,
        }
    except FileNotFoundError:
        return {"status": "unavailable", "bytes_verified": False}
    except OSError:
        return {"status": "unreadable_or_symlink", "bytes_verified": False}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def document_state(db, document_id, verify_local=False):
    read_snapshot(db)
    doc = get(db, m.Document, document_id)
    locations = []
    for row in (
        db.query(m.DocumentLocation)
        .filter_by(document_id=doc.id)
        .order_by(m.DocumentLocation.id)
    ):
        inspection = local_source(row.locator, verify_local)
        if inspection["bytes_verified"]:
            inspection["status"] = (
                "exact_hash_verified"
                if row.sha256 == inspection["sha256"]
                else "different_bytes" if row.sha256 else "no_registered_hash"
            )
        locations.append({**values(row), "inspection": inspection})
    result = {
        "document": values(doc),
        "locations": locations,
        "distinct_registered_hashes": sorted(
            {row["sha256"] for row in locations if row["sha256"]}
        ),
        "scope": "Historical registration is not proof of current byte availability. Different exports retain separate identity and coverage; no location is rewritten.",
    }
    result["state_digest"] = digest(result)
    return result


def relocate_document(db, data, actor):
    get(db, m.Document, data.document_id)
    if (
        not db.query(m.DocumentLocation)
        .filter_by(document_id=data.document_id, sha256=data.expected_sha256)
        .first()
    ):
        fail(
            "document_identity",
            "The expected original hash is not registered for this document",
        )
    checked = local_source(data.locator, True)
    if not checked["bytes_verified"] or checked.get("sha256") != data.expected_sha256:
        fail(
            "document_identity",
            "The relocated original must match the registered bytes exactly; register a different export separately",
            checked,
        )
    row = m.DocumentLocation(
        document_id=data.document_id, locator=data.locator, sha256=data.expected_sha256
    )
    db.add(row)
    db.flush()
    return {
        "document_id": data.document_id,
        "location_id": row.id,
        "sha256": data.expected_sha256,
        "content_verified": True,
        "size_bytes": checked["size_bytes"],
        "evidence": data.evidence,
        "original_locations_preserved": True,
        "financial_entries_added": False,
    }


def coverage_plan(db, account_id, start, end):
    from app.portfolio.reconciliation_evidence import (
        active_reconciliations,
        observation_inventory,
        supported_opening,
    )
    from app.portfolio.service import balance

    if start > end or (end - start).days > 36600:
        fail(
            "coverage_range",
            "Choose an ordered coverage interval no longer than 100 years",
        )
    read_snapshot(db)
    account = scope(db, account_id)
    if account.role != "bank":
        fail("coverage_account", "Use card_state for issuer coverage")
    rows = [
        row
        for row in active_reconciliations(db, account_id, date.max)
        if row.end >= start and row.start <= end
    ]
    intervals = []
    supported = []
    for row in rows:
        inventory = observation_inventory(db, account_id, row.start, row.end)
        reasons = []
        ids = [o["id"] for o in inventory]
        if any(not o["event_id"] for o in inventory) or sorted(ids) != sorted(
            (row.result or {}).get("observation_ids", [])
        ):
            reasons.append("stale_source_inventory")
        if (
            row.ending_cents - row.opening_cents
            != sum(o["amount_cents"] for o in inventory)
            or balance(db, account_id, row.end) != row.ending_cents
        ):
            reasons.append("ending_or_movement_difference")
        if supported_opening(db, account, row.start) != row.opening_cents:
            reasons.append("unsupported_opening")
        if not account.coverage_start or row.start < account.coverage_start:
            reasons.append("before_supported_opening")
        if any(
            other.id != row.id
            and other.start <= row.end
            and other.end >= row.start
            and (
                other.start,
                other.end,
                other.opening_cents,
                other.ending_cents,
                (other.result or {}).get("observation_ids"),
            )
            != (
                row.start,
                row.end,
                row.opening_cents,
                row.ending_cents,
                (row.result or {}).get("observation_ids"),
            )
            for other in rows
        ):
            reasons.append("competing_or_overlapping_statement")
        intervals.append(
            {
                "reconciliation_id": row.id,
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "document_id": row.document_id,
                "valid_against_current_book": not reasons,
                "blockers": reasons,
            }
        )
        if not reasons:
            supported.append((max(start, row.start), min(end, row.end)))
    cursor, missing = start, []
    for lo, hi in sorted(supported):
        if lo > cursor:
            missing.append(
                {
                    "start": cursor.isoformat(),
                    "end": (lo - timedelta(days=1)).isoformat(),
                    "kind": "leading" if cursor == start else "internal",
                }
            )
        if hi >= cursor:
            cursor = hi + timedelta(days=1) if hi < date.max else hi
    if cursor <= end and not any(hi == date.max for _, hi in supported):
        missing.append(
            {
                "start": cursor.isoformat(),
                "end": end.isoformat(),
                "kind": "trailing" if supported else "entire_requested_period",
            }
        )
    result = {
        "account_id": account_id,
        "entity_id": account.entity_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "coverage_start": (
            account.coverage_start.isoformat() if account.coverage_start else None
        ),
        "intervals": intervals,
        "missing_intervals": missing,
        "complete_against_book": not missing,
        "required_evidence": [
            "Original statements or exports for each missing interval",
            "Reconciled opening, activity and ending chain",
        ],
        "scope": "Import planning only; receiving receipts do not establish sender completeness or create sender cash/equity.",
    }
    result["plan_digest"] = digest(result)
    return result


def evidence_plan(db, observation_id):
    from app.portfolio.observation_state import observation_state

    read_snapshot(db)
    state = observation_state(db, observation_id)
    obs = get(db, m.BankObservation, observation_id)
    account = scope(db, obs.account_id)
    totals = defaultdict(lambda: defaultdict(int))
    for basis in ("accrual", "modified_cash_bank_posting"):
        for row in ledger(db, date.max, event_ids=state["event_ids"], basis=basis):
            if row["role"] in {
                "receivable",
                "payable",
                "custody_asset",
                "custody_liability",
                "due_from",
                "due_to",
            }:
                key = (
                    row["entity_id"],
                    row["account_id"],
                    row["role"],
                    *(row.get(k) for k in DIMENSIONS),
                )
                totals[key][basis] += row["debit_cents"] - row["credit_cents"]
    obligations = []
    for key, amounts in totals.items():
        if not any(amounts.values()):
            continue
        dims = dict(zip(DIMENSIONS, key[3:]))
        fund = db.get(m.Fund, dims["fund_id"]) if dims["fund_id"] else None
        obligations.append(
            {
                "entity_id": key[0],
                "account_id": key[1],
                "role": key[2],
                **dims,
                "amounts_by_basis": dict(amounts),
                "fund_beneficiary_id": fund.beneficiary_id if fund else None,
                "required_relationship_evidence": "Exact debtor/creditor and obligation basis; property ownership and fund beneficiary alone do not identify the debtor",
            }
        )
    card = db.query(m.CardIdentity).filter_by(account_id=obs.account_id).first()
    return {
        "observation_id": obs.id,
        "state_digest": state["state_digest"],
        "source": {
            "account_id": obs.account_id,
            "date": obs.date.isoformat(),
            "amount_cents": obs.amount_cents,
            "reference": obs.reference,
        },
        "open_reviews": [
            {"id": row["id"], "code": row["code"], "detail": row["detail"][:240]}
            for row in state["reviews"]
            if row["status"] == "open"
        ],
        "obligations": obligations,
        "funding_evidence_request": {
            "account_holder_entity_id": account.entity_id,
            "eligible_own_fund_ids": [
                f.id
                for f in db.query(m.Fund)
                .join(m.Party, m.Party.id == m.Fund.beneficiary_id)
                .filter(
                    m.Fund.holder_entity_id == account.entity_id,
                    m.Fund.purpose != "unresolved",
                    m.Fund.restriction == "unrestricted",
                    m.Party.entity_id == account.entity_id,
                )
                .order_by(m.Fund.id)
            ],
            "assessment_ref": (
                f"/api/portfolio/outgoing-funding-assessment?observation_id={obs.id}"
                if account.role == "bank"
                and obs.amount_cents < 0
                and obs.status == "posted"
                else None
            ),
            "ownership_status": "Unresolved until exact source evidence is reviewed; an eligible target is not a source conclusion",
            "coverage_start": (
                str(account.coverage_start) if account.coverage_start else None
            ),
            "required": [
                "Cash owner and eligible fund supported by dated source evidence",
                "Chronological coverage through the withdrawal",
            ],
            "eligibility_is_evidence": False,
        },
        "card_aliases": (
            [values(row) for row in db.query(m.CardAlias).filter_by(card_id=card.id)]
            if card
            else []
        ),
        "identity_rule": "Conflicting card suffixes need issuer replacement records with effective dates or an exact independent transaction reference; date/amount similarity is a lead only",
        "financial_entries_added": False,
    }
