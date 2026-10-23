"""Diagnostics report exact stored bytes and never run maintenance/writes."""

import sqlite3
from sqlalchemy import text, create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.portfolio.storage_diagnostics import storage_diagnostics
from tests.test_portfolio_runtime import synthetic_observations


def test_storage_read_only_exact_payload_bytes(db_session):
    synthetic_observations(db_session, 2)
    db_session.flush()
    connection = db_session.connection().connection.driver_connection
    forbidden = []
    write_ops = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
    }

    def authorize(action, arg1, arg2, database, trigger):
        if action in write_ops or (
            action == sqlite3.SQLITE_PRAGMA
            and arg1
            not in {
                "page_size",
                "page_count",
                "freelist_count",
                "journal_mode",
                "table_info",
                "database_list",
            }
        ):
            forbidden.append((action, arg1, arg2))
            return sqlite3.SQLITE_DENY
        if (
            action == sqlite3.SQLITE_PRAGMA
            and arg1 != "table_info"
            and arg2 is not None
        ):
            forbidden.append((action, arg1, arg2))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        result = storage_diagnostics(db_session)
    finally:
        connection.set_authorizer(None)
    assert forbidden == []
    assert result["diagnostic_only"]
    requests = next(row for row in result["payloads"] if row["table"] == "re_requests")
    assert (
        requests["json_bytes_by_column"]["result"]
        == db_session.execute(
            text("SELECT sum(length(CAST(result AS BLOB))) FROM re_requests")
        ).scalar_one()
    )
    assert result["retention"]["automatic_deletion"] is False
    assert result["database"]["wal_bytes"] == 0
    if result["dbstat_available"]:
        assert any(row["name"] == "re_requests" for row in result["object_footprints"])
        assert all(
            row["allocated_bytes"] >= row["payload_bytes"]
            for row in result["object_footprints"]
        )


def test_storage_wal_reports_file_without_checkpoint(tmp_path):
    path = tmp_path / "diagnostics.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        synthetic_observations(db, 2)
        db.commit()
        wal = path.with_name(path.name + "-wal")
        size = wal.stat().st_size
        result = storage_diagnostics(db)
        assert result["database"]["journal_mode"] == "wal"
        assert result["database"]["wal_bytes"] == size > 0
        assert wal.stat().st_size == size
    engine.dispose()


def test_storage_without_dbstat_is_explicit(db_session, monkeypatch):
    from sqlalchemy.exc import OperationalError

    connection = db_session.connection()
    original = connection.exec_driver_sql

    def unavailable(sql, *args, **kwargs):
        if "FROM dbstat" in sql:
            raise OperationalError(sql, (), Exception("no such table: dbstat"))
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(connection, "exec_driver_sql", unavailable)
    result = storage_diagnostics(db_session)
    assert result["dbstat_available"] is False
    assert result["object_footprints"] is None
    assert result["database"]["page_count"] > 0
