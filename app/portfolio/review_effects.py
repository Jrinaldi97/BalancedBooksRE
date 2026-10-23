"""Request-scoped review effects from ORM flushes, without whole-book scans."""

from collections import defaultdict
from sqlalchemy import event, inspect, select
from app.portfolio import models as m


class ReviewEffects:
    """Capture first status and final status for every review touched in a unit.

    Service review mutations use ORM objects. Register after entering the command
    savepoint so preexisting session changes are not attributed to this command.
    Nested command collectors are independent; an outer batch sees final effects.
    No persistent cache or process-global listener is installed.
    """

    def __init__(self, db):
        self.db = db
        self.before = {}

    def __enter__(self):
        event.listen(self.db, "before_flush", self.capture)
        return self

    def __exit__(self, *exc):
        event.remove(self.db, "before_flush", self.capture)

    def capture(self, session, flush_context, instances):
        for row in set(session.new) | set(session.dirty):
            if not isinstance(row, m.ReviewItem) or row in self.before:
                continue
            state = inspect(row)
            history = state.attrs.status.history
            if row in session.new:
                self.before[row] = None
            elif history.has_changes():
                if history.deleted:
                    previous = history.deleted[0]
                else:
                    # An expired attribute can have no history. Read just this
                    # row before the pending UPDATE, without ORM autoflush.
                    previous = (
                        session.connection()
                        .execute(
                            select(m.ReviewItem.status).where(m.ReviewItem.id == row.id)
                        )
                        .scalar_one()
                    )
                self.before[row] = previous

    def result(self):
        changed = sorted(
            (r for r, old in self.before.items() if old != r.status),
            key=lambda r: r.id,
        )
        closed = [
            r.id for r in changed if self.before[r] == "open" and r.status == "resolved"
        ]
        opened = [
            r.id for r in changed if self.before[r] is None and r.status == "open"
        ]
        reopened = [
            r.id for r in changed if self.before[r] == "resolved" and r.status == "open"
        ]
        by_code = defaultdict(
            lambda: {
                "gross_closures": 0,
                "new_open_count": 0,
                "reopened_count": 0,
                "net_open_reduction": 0,
            }
        )
        for row in changed:
            counts = by_code[row.code]
            counts["gross_closures"] += row.id in closed
            counts["new_open_count"] += row.id in opened
            counts["reopened_count"] += row.id in reopened
            counts["net_open_reduction"] = (
                counts["gross_closures"]
                - counts["new_open_count"]
                - counts["reopened_count"]
            )
        return {
            "preexisting_closed_ids": closed,
            "new_open_ids": opened,
            "reopened_ids": reopened,
            "gross_closures": len(closed),
            "new_open_count": len(opened),
            "reopened_count": len(reopened),
            "net_open_reduction": len(closed) - len(opened) - len(reopened),
            "by_code": dict(sorted(by_code.items())),
            "transitions": [
                {
                    "review_id": r.id,
                    "previous_status": self.before[r],
                    "status": r.status,
                    "code": r.code,
                    "observation_id": r.observation_id,
                    "event_id": r.event_id,
                    "detail": r.detail,
                    "replaces_review_ids": closed if r.id in opened else [],
                }
                for r in changed
            ],
        }
