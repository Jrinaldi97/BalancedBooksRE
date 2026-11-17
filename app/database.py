# ============================================================================
# Database layer — SQLAlchemy engine + session factory.
# PostgreSQL in server mode, per-company SQLite files in desktop mode.
# ============================================================================

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from starlette.requests import HTTPConnection

from app.config import DATABASE_URL

# Pool tuning rationale (Phase 9.6 perf pass):
#   pool_size=10        small base pool; most requests are short-lived
#   max_overflow=20     burst capacity when analytics + concurrent users hit
#   pool_recycle=1800   recycle every 30 min to avoid stale TCP idle kills
#   pool_pre_ping=True  cheap SELECT 1 before each checkout; catches dead conns
#   pool_use_lifo=True  reuse hottest conn first -> better CPU cache locality
# File-backed SQLite uses a connection for each worker. In-memory SQLite keeps its pool class.
_is_sqlite = DATABASE_URL.startswith("sqlite")
_engine_kwargs = dict(pool_pre_ping=True)
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        pool_use_lifo=True,
    )
elif (
    make_url(DATABASE_URL).database not in (None, "", ":memory:", "file::memory:")
    and make_url(DATABASE_URL).query.get("mode") != "memory"
):
    _engine_kwargs.update(pool_size=40, max_overflow=10)
engine = create_engine(DATABASE_URL, **_engine_kwargs)


def enable_sqlite_tuning(target_engine) -> None:
    """Concurrency PRAGMAs for SQLite engines (Server Edition groundwork).

    WAL lets readers proceed while one writer commits — the difference
    between "works for an office" and "database is locked" the moment a
    second person opens a report mid-save. busy_timeout makes brief lock
    contention wait instead of erroring; NORMAL sync is the recommended
    pairing with WAL. Harmless no-ops on :memory: databases.

    secure_delete: a company keeps its files in its own database (W-4s and
    I-9s among them), and SQLite otherwise leaves a deleted row's bytes in
    the file's free pages, where a later copy or backup of the file still
    carries them. With it on, a deleted document is overwritten, not just
    unlinked from the table.
    """
    from sqlalchemy import event

    @event.listens_for(target_engine, "connect")
    def _tune(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA secure_delete=ON")
        cur.close()


if _is_sqlite:
    enable_sqlite_tuning(engine)


def _is_sqlite_bind(bind) -> bool:
    return getattr(getattr(bind, "dialect", None), "name", "") == "sqlite"


def begin_write(session) -> None:
    """Take SQLite's write lock now (BEGIN IMMEDIATE) if this session is on
    SQLite and has not begun a transaction on its connection yet; a no-op
    on any other database."""
    bind = session.get_bind()
    if not _is_sqlite_bind(bind):
        return
    raw = session.connection().connection.dbapi_connection
    if getattr(raw, "in_transaction", False):
        return
    raw.execute("BEGIN IMMEDIATE")


def _lock_rows_on_sqlite(orm_execute_state) -> None:
    stmt = orm_execute_state.statement
    if getattr(stmt, "_for_update_arg", None) is None:
        return
    begin_write(orm_execute_state.session)


event.listen(Session, "do_orm_execute", _lock_rows_on_sqlite)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db(request: HTTPConnection = None):
    """Request-scoped DB session (FastAPI dependency).

    When FastAPI injects the request, the acting username is stamped onto
    the Session's own info dict — the audit hooks receive this exact
    object at flush time, so attribution travels WITH the session instead
    of relying on contextvar propagation (which proved unreliable on the
    frozen Windows runtime; the contextvar remains as a fallback for
    sessions created outside the request cycle)."""
    db = SessionLocal()
    try:
        session = getattr(request, "session", None) if request is not None else None
        if isinstance(session, dict) and session.get("authenticated") is True:
            db.info["acting_username"] = session.get("username") or "operator"
            # The closing-date override password, when the page resent a
            # refused change with it. A signed-in person's request only —
            # the token branch below never carries it.
            from app.services.closing_date import (
                PASSWORD_HEADER,
                SESSION_INFO_KEY,
                password_from_header,
            )

            supplied = password_from_header(request.headers.get(PASSWORD_HEADER))
            if supplied:
                db.info[SESSION_INFO_KEY] = supplied
        elif request is not None:
            # Scoped API tokens: the middleware stashes the principal on
            # request.state — audit rows attribute to "token:<label>".
            tp = getattr(getattr(request, "state", None), "token_principal", None)
            if isinstance(tp, dict) and tp.get("username"):
                db.info["acting_username"] = tp["username"]
    except Exception:
        pass  # attribution must never break a request
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
