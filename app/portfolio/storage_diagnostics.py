"""Read-only SQLite footprint diagnostics; no cleanup or maintenance actions."""

from pathlib import Path
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.portfolio.reports import read_snapshot
from app.portfolio.service import fail

# Fixed SQL identifiers, never supplied by clients. Byte lengths are stored JSON
# bytes, not reconstructed JSON or an estimate of SQLite page usage.
PAYLOAD_COLUMNS = {
    "re_requests": ("result",),
    "re_proposals": ("request", "revisions", "preview", "committed_result"),
    "re_events": ("source",),
    "re_bank_observations": ("source",),
    "re_report_runs": ("parameters", "result"),
    "re_agent_operations": ("request", "binding", "authorization", "error"),
    "re_operation_transitions": ("detail",),
    "re_operation_verifications": ("source_documents",),
    "re_card_coverage": ("observation_ids",),
    "re_card_activities": ("components",),
}
TEXT_COLUMNS = {
    "re_events": ("description", "rationale"),
    "re_review_items": ("detail", "resolution"),
    "re_documents": ("title", "provider_id"),
    "re_document_locations": ("locator",),
    "re_operation_verifications": ("proof_reference", "summary"),
    "re_card_coverage": ("evidence",),
    "re_card_activities": ("evidence",),
}


def storage_diagnostics(db):
    if db.get_bind().dialect.name != "sqlite":
        fail("unsupported_database", "Storage diagnostics currently require SQLite")
    with db.no_autoflush:
        read_snapshot(db)
        connection = db.connection()
        pragmas = {
            name: connection.exec_driver_sql("PRAGMA " + name).scalar_one()
            for name in ("page_size", "page_count", "freelist_count", "journal_mode")
        }
        schema = {
            row.name: {"kind": row.type, "table": row.tbl_name}
            for row in connection.exec_driver_sql(
                "SELECT name, type, tbl_name FROM sqlite_schema WHERE type IN ('table', 'index')"
            )
        }
        footprints = None
        try:
            footprints = [
                {
                    "name": row.name,
                    **schema.get(row.name, {"kind": "internal", "table": row.name}),
                    "pages": row.pages,
                    "allocated_bytes": row.allocated_bytes,
                    "payload_bytes": row.payload_bytes,
                    "unused_bytes": row.unused_bytes,
                }
                for row in connection.exec_driver_sql(
                    "SELECT name, count(*) AS pages, sum(pgsize) AS allocated_bytes, "
                    "sum(payload) AS payload_bytes, sum(unused) AS unused_bytes "
                    "FROM dbstat GROUP BY name ORDER BY allocated_bytes DESC, name"
                )
            ]
        except OperationalError as exc:
            if "no such table: dbstat" not in str(exc):
                raise
        payloads = []
        for table in dict.fromkeys([*PAYLOAD_COLUMNS, *TEXT_COLUMNS]):
            columns = (*PAYLOAD_COLUMNS.get(table, ()), *TEXT_COLUMNS.get(table, ()))
            if table not in schema:
                continue
            # Some schema revisions omit optional JSON columns. Discover names
            # from the database; neither identifiers nor SQL come from requests.
            present = {
                row.name
                for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
            }
            columns = [name for name in columns if name in present]
            expressions = ", ".join(
                f'coalesce(sum(length(CAST("{name}" AS BLOB))), 0) AS "{name}"'
                for name in columns
            )
            row = (
                connection.execute(
                    text(
                        f'SELECT count(*) AS rows{", " if expressions else ""}{expressions} FROM "{table}"'
                    )
                )
                .mappings()
                .one()
            )
            payloads.append(
                {
                    "table": table,
                    "rows": row["rows"],
                    "json_bytes_by_column": {
                        name: row[name]
                        for name in columns
                        if name in PAYLOAD_COLUMNS.get(table, ())
                    },
                    "text_bytes_by_column": {
                        name: row[name]
                        for name in columns
                        if name in TEXT_COLUMNS.get(table, ())
                    },
                }
            )
        location = next(
            (
                row.file
                for row in connection.exec_driver_sql("PRAGMA database_list")
                if row.name == "main"
            ),
            None,
        )
        file_bytes = wal_bytes = None
        if location:
            try:
                file_bytes = Path(location).stat().st_size
                wal_path = Path(location + "-wal")
                wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
            except OSError:
                pass  # Availability is explicit; do not turn it into a zero.
        return {
            "diagnostic_only": True,
            "database": {
                **pragmas,
                "logical_bytes": pragmas["page_count"] * pragmas["page_size"],
                "freelist_bytes": pragmas["freelist_count"] * pragmas["page_size"],
                "file_bytes": file_bytes,
                "wal_bytes": wal_bytes,
                "file_measurement": "Filesystem sizes are sampled separately and can change during concurrent writes",
            },
            "object_footprints": footprints,
            "dbstat_available": footprints is not None,
            "dbstat_scope": "B-tree pages only; excludes freelist, pointer-map and lock pages",
            "payloads": payloads,
            "retention": {
                "automatic_deletion": False,
                "audit_history": "Retain events, requests, proposals, source identities and correction lineage",
                "maintenance": "This read performs no checkpoint, vacuum, deletion or journal-mode change",
            },
        }
