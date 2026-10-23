"""Small synthetic-only before/after revision-loader benchmark; no DB arguments.

Run with python -m tests.benchmark_revision_reads from this checkout. Requires
local Git history for the pinned baseline and the normal test dependencies.
"""

from contextlib import redirect_stdout
from datetime import date, timedelta
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from types import ModuleType

with redirect_stdout(sys.stderr):
    import tests.conftest  # noqa: F401 -- isolate all app paths before imports
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models.transactions import TransactionLine
from app.portfolio import models as m
from app.portfolio.ledger_snapshot import LedgerSnapshot
from app.portfolio.service import digest
from tests.test_portfolio_runtime import synthetic_observations

BASELINE = "5da53e6"


def measure(db, loader, event_ids):
    samples = []
    output = None
    for _ in range(5):
        db.expunge_all()
        queries = []

        def count(*args):
            queries.append(args[2])

        event.listen(db.get_bind(), "before_cursor_execute", count)
        tracemalloc.start()
        started = time.perf_counter()
        try:
            packet = loader(db, date.max, event_ids=event_ids)
            output = [
                packet.project_bases(day)
                for day in (date(2026, 1, 10), date(2026, 1, 31), date.max)
            ]
            encoded = json.dumps(output, sort_keys=True).encode()
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            event.remove(db.get_bind(), "before_cursor_execute", count)
        samples.append(
            {
                "elapsed_ms": round(elapsed * 1000, 3),
                "peak_python_bytes": peak,
                "queries": len(queries),
                "json_bytes": len(encoded),
            }
        )
        del packet
    return output, {
        "samples": samples,
        "p50_elapsed_ms": statistics.median(s["elapsed_ms"] for s in samples),
        "p95_elapsed_ms": max(s["elapsed_ms"] for s in samples),
    }


def main():
    reference = ModuleType("frozen_revision_loader")
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:app/portfolio/ledger_snapshot.py"], text=True
    )
    exec(compile(source, "frozen_revision_loader", "exec"), reference.__dict__)
    with tempfile.TemporaryDirectory(prefix="bbre-revision-benchmark-") as directory:
        engine = create_engine(f"sqlite:///{directory}/synthetic.db")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            _, bank, _ = synthetic_observations(db, 30)
            original_lines = (
                db.query(TransactionLine.id, m.JournalScope.event_id)
                .join(
                    m.JournalScope,
                    m.JournalScope.transaction_id == TransactionLine.transaction_id,
                )
                .filter(TransactionLine.account_id == bank)
                .all()
            )
            prose = "Synthetic source evidence retained in the audit record. " * 160
            for line_id, event_id in original_lines:
                for index in range(20):
                    day = date(2026, 1, 2) + timedelta(days=index)
                    db.add(
                        m.CashClassification(
                            line_id=line_id,
                            event_id=event_id,
                            date=day,
                            cash_flow="financing",
                            reason=prose,
                        )
                    )
                    db.add(
                        m.LineAllocation(
                            line_id=line_id,
                            event_id=event_id,
                            date=day,
                            evidence=prose,
                            components=[
                                {
                                    "amount_cents": 50,
                                    "item": f"component-a-{index}",
                                    "cash_flow": "operating",
                                },
                                {
                                    "amount_cents": 50,
                                    "item": f"component-b-{index}",
                                    "cash_flow": "investing",
                                },
                            ],
                        )
                    )
            db.commit()
            event_ids = [row.event_id for row in original_lines]
            before, old_metrics = measure(db, reference.LedgerSnapshot, event_ids)
            after, new_metrics = measure(db, LedgerSnapshot, event_ids)
            assert before == after
            print(
                json.dumps(
                    {
                        "scope": "Fresh synthetic SQLite; 30 observations, 20 allocation and 20 classification revisions per source line, 5 ORM-evicted samples; 3 dated projections on both bases. Includes serialization; OS cache uncontrolled; excludes production/network/writer-lock/RSS measurements.",
                        "baseline_commit": subprocess.check_output(
                            ["git", "rev-parse", BASELINE], text=True
                        ).strip(),
                        "revision_rows": len(original_lines) * 40,
                        "stored_prose_bytes": len(original_lines)
                        * 40
                        * len(prose.encode()),
                        "frozen_baseline": old_metrics,
                        "streamed_financial_columns": new_metrics,
                        "all_output_fields_equal": True,
                        "output_digest": digest(after),
                    },
                    indent=2,
                )
            )
        engine.dispose()


if __name__ == "__main__":
    main()
