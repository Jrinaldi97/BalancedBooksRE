"""Synthetic-only runtime measurements; requires development/test dependencies.

Run from the checkout with python scripts/benchmark_portfolio_runtime.py.
Never accepts a database path: tests.conftest isolates every app data directory,
and this script creates a fresh temporary SQLite database. Measurements report
Python/SQL/serialization costs, not MCP transit, production lock time or OS-cold IO.
"""

import argparse
from contextlib import redirect_stdout
from datetime import date
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with redirect_stdout(sys.stderr):
    import tests.conftest  # noqa: E402,F401 -- isolate settings before app imports
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.transactions import TransactionLine  # noqa: E402
from app.portfolio import models as m  # noqa: E402
from app.portfolio.service import digest  # noqa: E402
from app.portfolio.observation_state import observation_states  # noqa: E402
from app.portfolio.runtime import project_state  # noqa: E402
from tests.reference_portfolio_readers import baseline_observation_state  # noqa: E402
from tests.test_portfolio_runtime import synthetic_observations  # noqa: E402


def measure(db, work, repeats, *, evict=True):
    samples = []
    result = None
    for iteration in range(repeats):
        # Evict ORM identities only; this is not an OS page-cache reset.
        if evict:
            db.expunge_all()
        queries = []
        starts = []

        def before(*args):
            starts.append(time.perf_counter())

        def after(*args):
            queries.append(time.perf_counter() - starts.pop())

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
        tracemalloc.start()
        started = time.perf_counter()
        try:
            result = work()
            encoded = json.dumps(result, default=str, sort_keys=True).encode()
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            event.remove(engine, "before_cursor_execute", before)
            event.remove(engine, "after_cursor_execute", after)
        samples.append(
            {
                "elapsed_ms": round(elapsed * 1000, 3),
                "sql_ms": round(sum(queries) * 1000, 3),
                "queries": len(queries),
                "json_bytes": len(encoded),
                "peak_python_bytes": peak,
            }
        )
    elapsed = sorted(sample["elapsed_ms"] for sample in samples)
    return result, {
        "samples": samples,
        "p50_elapsed_ms": statistics.median(elapsed),
        "p95_elapsed_ms": elapsed[min(len(elapsed) - 1, int(len(elapsed) * 0.95))],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=int, default=300)
    parser.add_argument("--overrides-per-observation", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if (
        args.observations < 200
        or args.repeats < 2
        or args.overrides_per_observation < 1
    ):
        parser.error("At least 200 observations, 2 repeats and 1 override required")
    output = {
        "scope": "Fresh synthetic SQLite; both ORM-evicted and warm repeated samples, with OS page cache uncontrolled; serialization included; no network, writer-lock or production timing claims",
        "parameters": vars(args),
        "scoped_overrides": {},
        "observation_states": {},
    }
    with tempfile.TemporaryDirectory(prefix="bbre-runtime-benchmark-") as directory:
        engine = create_engine(f"sqlite:///{directory}/synthetic.db")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        with factory() as db:
            _, bank, ids = synthetic_observations(db, args.observations)
            lines = db.query(TransactionLine).filter_by(account_id=bank).all()
            for line in lines:
                event_id = (
                    db.query(m.JournalScope.event_id)
                    .filter_by(transaction_id=line.transaction_id)
                    .scalar()
                )
                db.add_all(
                    [
                        m.CashClassification(
                            line_id=line.id,
                            event_id=event_id,
                            date=date(2026, 1, 2),
                            cash_flow="financing",
                            reason="Synthetic repeated evidence " * 10,
                        )
                        for _ in range(args.overrides_per_observation)
                    ]
                )
            db.commit()
            target_line = lines[0].id

            def classifications(scoped):
                query = db.query(m.CashClassification).filter(
                    m.CashClassification.date <= date(2026, 1, 31)
                )
                if scoped:
                    query = query.filter(m.CashClassification.line_id == target_line)
                # Same before/after effective-date ordering as reports.ledger.
                found = {
                    row.line_id: {"event_id": row.event_id, "cash_flow": row.cash_flow}
                    for row in query.order_by(
                        m.CashClassification.date, m.CashClassification.id
                    )
                }
                return {target_line: found[target_line]}

            before, baseline = measure(db, lambda: classifications(False), args.repeats)
            after, optimized = measure(db, lambda: classifications(True), args.repeats)
            assert before == after
            output["scoped_overrides"] = {
                "baseline_all_rows": baseline,
                "scoped_rows": optimized,
                "output_digest": digest(after),
                "equivalent": True,
                "baseline_rows": len(lines) * args.overrides_per_observation,
                "scoped_rows_loaded": args.overrides_per_observation,
            }
            for count in (1, 30, 100, 200):

                def packet(states, projection="full"):
                    return {
                        "projection": projection,
                        "count": len(states),
                        "truncated": False,
                        "states": [
                            project_state(state, projection) for state in states
                        ],
                        "snapshot_digest": digest(
                            [
                                {
                                    "observation_id": state["observation"]["id"],
                                    "state_digest": state["state_digest"],
                                }
                                for state in states
                            ]
                        ),
                        "snapshot_scope": "One SQLite read snapshot for this response; other database engines follow their configured isolation. Digest is not a cross-request snapshot cursor.",
                    }

                before, before_metrics = measure(
                    db,
                    lambda: packet(
                        [baseline_observation_state(db, ident) for ident in ids[:count]]
                    ),
                    args.repeats,
                )
                full, full_metrics = measure(
                    db,
                    lambda: packet(observation_states(db, ids[:count])),
                    args.repeats,
                )
                compact, compact_metrics = measure(
                    db,
                    lambda: packet(observation_states(db, ids[:count]), "compact"),
                    args.repeats,
                )
                assert before == full
                assert full["snapshot_digest"] == compact["snapshot_digest"]
                for left, right in zip(full["states"], compact["states"]):
                    for field in (
                        "state_digest",
                        "event_ids",
                        "suspense_residuals",
                        "bank_movements",
                        "cash_lineage",
                    ):
                        assert left[field] == right[field]
                warm_before, warm_before_metrics = measure(
                    db,
                    lambda: packet(
                        [baseline_observation_state(db, ident) for ident in ids[:count]]
                    ),
                    args.repeats,
                    evict=False,
                )
                warm_full, warm_full_metrics = measure(
                    db,
                    lambda: packet(observation_states(db, ids[:count])),
                    args.repeats,
                    evict=False,
                )
                warm_compact, warm_compact_metrics = measure(
                    db,
                    lambda: packet(observation_states(db, ids[:count]), "compact"),
                    args.repeats,
                    evict=False,
                )
                assert warm_before == warm_full == full
                assert warm_compact == compact
                output["observation_states"][str(count)] = {
                    "frozen_baseline_full": before_metrics,
                    "shared_full": full_metrics,
                    "shared_compact": compact_metrics,
                    "warm_frozen_baseline_full": warm_before_metrics,
                    "warm_shared_full": warm_full_metrics,
                    "warm_shared_compact": warm_compact_metrics,
                    "all_output_fields_and_digest_parity": True,
                    "output_digest": digest(full),
                }
        engine.dispose()
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
