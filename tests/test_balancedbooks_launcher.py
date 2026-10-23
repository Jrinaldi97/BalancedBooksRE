import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from balancedbooks_launcher import company_paths, selected_company


def test_company_files_have_separate_database_credential_and_listener(tmp_path):
    (tmp_path / "companies").mkdir()
    for name in ("alpha.db", "beta.db"):
        (tmp_path / "companies" / name).touch()
    original = company_paths(tmp_path)
    alpha = company_paths(tmp_path, "alpha.db")
    beta = company_paths(tmp_path, "beta.db")
    assert original[0] == tmp_path / "portfolio.db"
    assert len({item[0] for item in (original, alpha, beta)}) == 3
    assert len({item[1] for item in (original, alpha, beta)}) == 3
    assert len({item[2] for item in (original, alpha, beta)}) == 3
    assert alpha == company_paths(tmp_path, "alpha.db")


@pytest.mark.parametrize("name", ["../outside.db", "/tmp/outside.db", "missing.db"])
def test_refuse_invalid_or_missing_company(tmp_path, name):
    with pytest.raises(ValueError):
        company_paths(tmp_path, name)


def test_refuse_symlink_company(tmp_path):
    (tmp_path / "companies").mkdir()
    outside = tmp_path / "outside.db"
    outside.touch()
    (tmp_path / "companies" / "alias.db").symlink_to(outside)
    with pytest.raises(ValueError):
        company_paths(tmp_path, "alias.db")


def test_reopen_selected_company_without_replacing_original(tmp_path):
    assert selected_company(tmp_path) == "portfolio.db"
    (tmp_path / "companies").mkdir()
    (tmp_path / "companies" / "alpha.db").touch()
    (tmp_path / "active-company.json").write_text(json.dumps({"file": "alpha.db"}))
    assert selected_company(tmp_path) == "alpha.db"
    (tmp_path / "companies" / "alpha.db").unlink()
    with pytest.raises(ValueError):
        selected_company(tmp_path)


def test_native_identity_creation_is_serialized_stable_and_company_specific(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.database as database
    from app.models.settings import Settings
    from balancedbooks_launcher import ensure_company_identity

    identities = []
    for name in ("first", "second"):
        engine = create_engine(f"sqlite:///{tmp_path / (name + '.db')}")
        Settings.__table__.create(engine)
        factory = sessionmaker(bind=engine)
        monkeypatch.setattr(database, "SessionLocal", factory)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: ensure_company_identity(), range(2)))
        assert results[0] == results[1] and len(results[0]) == 32
        with factory() as db:
            before = [
                (row.id, row.key, row.value, row.updated_at)
                for row in db.query(Settings)
            ]
        assert ensure_company_identity() == results[0]
        with factory() as db:
            assert [
                (row.id, row.key, row.value, row.updated_at)
                for row in db.query(Settings)
            ] == before
        identities.append(results[0])
        engine.dispose()
    assert identities[0] != identities[1]


@pytest.fixture
def native_credentials(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Native setup uses POSIX/macOS file locks")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.database as database
    from app.models.api_tokens import ApiToken
    from app.models.audit import AuditLog

    engine = create_engine(f"sqlite:///{tmp_path / 'native.db'}")
    ApiToken.__table__.create(engine)
    AuditLog.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    yield tmp_path / "ai-connection.token", factory
    engine.dispose()


def _token_snapshot(factory):
    from app.models.api_tokens import ApiToken
    from app.models.audit import AuditLog

    with factory() as db:
        return (
            [
                tuple(getattr(row, col.name) for col in ApiToken.__table__.columns)
                for row in db.query(ApiToken)
            ],
            db.query(AuditLog).count(),
        )


def test_concurrent_native_credential_calls_create_one_audited_secret(
    native_credentials,
):
    from concurrent.futures import ThreadPoolExecutor
    from app.services.api_token_service import hash_token
    from balancedbooks_launcher import ensure_local_token
    from app.models.api_tokens import ApiToken
    from app.models.audit import AuditLog

    path, factory = native_credentials
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda _: ensure_local_token(path), range(8)))
    original = path.read_bytes()
    with factory() as db:
        assert db.query(ApiToken).count() == 1
        assert db.query(ApiToken).one().token_hash == hash_token(original.decode())
        assert db.query(AuditLog).count() == 1
        assert db.query(AuditLog).one().action == "INSERT"
    before = _token_snapshot(factory)
    ensure_local_token(path)
    assert path.read_bytes() == original
    assert _token_snapshot(factory) == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name(path.name + ".pending").exists()


@pytest.mark.parametrize("committed", [False, True])
def test_native_issuance_recovers_before_or_after_lost_commit_acknowledgment(
    native_credentials, monkeypatch, committed
):
    import balancedbooks_launcher as launcher

    path, factory = native_credentials
    original_commit = factory.class_.commit

    def failing_commit(db):
        if committed:
            original_commit(db)
        raise OSError("Injected commit boundary interruption")

    with monkeypatch.context() as patch:
        patch.setattr(factory.class_, "commit", failing_commit)
        with pytest.raises(OSError, match="commit boundary"):
            launcher.ensure_local_token(path)
    pending = path.with_name(path.name + ".pending")
    staged = json.loads(pending.read_text())["secret"]
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert not path.exists()
    rows, audit_count = _token_snapshot(factory)
    assert len(rows) == audit_count == int(committed)
    launcher.ensure_local_token(path)
    assert path.read_text() == staged
    rows, audit_count = _token_snapshot(factory)
    assert len(rows) == audit_count == 1
    assert not pending.exists()


def _interrupt_final_publication(monkeypatch, path):
    import balancedbooks_launcher as launcher

    original = launcher._atomic_create

    def fail_final(target, contents):
        if target == path:
            raise OSError("Injected final publication failure")
        return original(target, contents)

    with monkeypatch.context() as patch:
        patch.setattr(launcher, "_atomic_create", fail_final)
        with pytest.raises(OSError, match="publication"):
            launcher.ensure_local_token(path)


def test_native_publication_recovery_preserves_current_role_and_metadata(
    native_credentials, monkeypatch
):
    import balancedbooks_launcher as launcher
    from app.models.api_tokens import ApiToken

    path, factory = native_credentials
    _interrupt_final_publication(monkeypatch, path)
    with factory() as db:
        row = db.query(ApiToken).one()
        row.role = "readonly"
        row.created_by = "reviewed-by-administrator"
        db.commit()
    before = _token_snapshot(factory)
    launcher.ensure_local_token(path)
    assert path.exists()
    assert _token_snapshot(factory) == before


@pytest.mark.parametrize("change", ["revoked", "mismatched"])
def test_native_pending_recovery_never_reactivates_or_replaces_token(
    native_credentials, monkeypatch, change
):
    import balancedbooks_launcher as launcher
    from app.models.api_tokens import ApiToken

    path, factory = native_credentials
    _interrupt_final_publication(monkeypatch, path)
    with factory() as db:
        row = db.query(ApiToken).one()
        if change == "revoked":
            row.is_active = False
        else:
            row.token_hash = "a" * 64
        db.commit()
    before = _token_snapshot(factory)
    with pytest.raises(RuntimeError, match="existing or revoked"):
        launcher.ensure_local_token(path)
    assert not path.exists()
    assert _token_snapshot(factory) == before


@pytest.mark.parametrize("active", [True, False])
def test_preexisting_orphan_database_token_requires_explicit_recovery(
    native_credentials, active
):
    from app.models.api_tokens import ApiToken
    from balancedbooks_launcher import ensure_local_token

    path, factory = native_credentials
    with factory() as db:
        db.add(
            ApiToken(
                label="BalancedBooksRE local Codex",
                role="readonly",
                is_active=active,
                token_hash="a" * 64,
                token_hint="sbp_orphan",
            )
        )
        db.commit()
    before = _token_snapshot(factory)
    with pytest.raises(RuntimeError, match="secret is missing"):
        ensure_local_token(path)
    assert _token_snapshot(factory) == before
    assert not path.exists()
    assert not path.with_name(path.name + ".pending").exists()


def test_postpublication_interruption_and_revocation_preserve_existing_file_bytes(
    native_credentials, monkeypatch
):
    import balancedbooks_launcher as launcher
    from app.models.api_tokens import ApiToken

    path, factory = native_credentials
    with monkeypatch.context() as patch:

        def fail_cleanup(_):
            raise OSError("Injected pending cleanup interruption")

        patch.setattr(launcher, "_remove_pending", fail_cleanup)
        with pytest.raises(OSError, match="cleanup"):
            launcher.ensure_local_token(path)
    original = path.read_bytes() + b"\n"
    path.write_bytes(original)
    with factory() as db:
        db.query(ApiToken).one().is_active = False
        db.commit()
    before = _token_snapshot(factory)
    launcher.ensure_local_token(path)
    assert path.read_bytes() == original
    assert _token_snapshot(factory) == before
    assert not path.with_name(path.name + ".pending").exists()


def test_native_atomic_file_failure_leaves_no_partial_settings_or_token(
    native_credentials, monkeypatch
):
    import balancedbooks_launcher as launcher

    path, factory = native_credentials
    settings = path.parent / "settings.env"
    with monkeypatch.context() as patch:

        def fail_link(*_):
            raise OSError("Injected publication error")

        patch.setattr(launcher.os, "link", fail_link)
        with pytest.raises(OSError, match="publication"):
            launcher._ensure_settings(settings)
        with pytest.raises(OSError, match="publication"):
            launcher.ensure_local_token(path)
    assert not settings.exists() and not path.exists()
    assert [file.name for file in path.parent.glob(".native-*")] == [
        ".native-setup.lock"
    ]
    assert _token_snapshot(factory) == ([], 0)
    launcher._ensure_settings(settings)
    content = settings.read_bytes()
    launcher._ensure_settings(settings)
    assert settings.read_bytes() == content
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


def test_prepare_holds_setup_lock_and_preserves_complete_settings(
    native_credentials, monkeypatch
):
    import fcntl
    import balancedbooks_launcher as launcher

    path, _ = native_credentials
    settings = path.parent / "settings.env"
    content = b"# Existing deployment settings\nPAYROLL_ENCRYPTION_SECRET=existing-payroll\nSESSION_SECRET_KEY=existing-session\nCUSTOM_VALUE=keep-this\n"
    settings.write_bytes(content)

    def prepared_while_locked(data_dir, filename):
        fd = os.open(data_dir / ".native-setup.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        launcher._ensure_settings(settings)
        launcher._ensure_local_token_locked(path)
        return data_dir

    monkeypatch.setattr(launcher, "_prepare_locked", prepared_while_locked)
    assert launcher.prepare(path.parent) == path.parent
    assert settings.read_bytes() == content


def test_existing_final_credential_conflict_does_not_consume_pending_recovery(
    native_credentials, monkeypatch
):
    import balancedbooks_launcher as launcher
    from app.services.api_token_service import generate_token

    path, factory = native_credentials
    _interrupt_final_publication(monkeypatch, path)
    content = generate_token().encode() + b"\n"
    path.write_bytes(content)
    before = _token_snapshot(factory)
    pending = path.with_name(path.name + ".pending")
    staged = pending.read_bytes()
    with pytest.raises(RuntimeError, match="conflicts with the existing"):
        launcher.ensure_local_token(path)
    assert path.read_bytes() == content
    assert pending.read_bytes() == staged
    assert _token_snapshot(factory) == before


@pytest.mark.parametrize(
    "name,contents",
    [("settings.env", b"SESSION_SECRET_KEY=partial\n"), ("ai-connection.token", b"")],
)
def test_existing_incomplete_native_files_are_never_silently_replaced(
    native_credentials, name, contents
):
    import balancedbooks_launcher as launcher

    path, factory = native_credentials
    target = path.parent / name
    target.write_bytes(contents)
    with pytest.raises(RuntimeError, match="incomplete"):
        if name == "settings.env":
            launcher._ensure_settings(target)
        else:
            launcher.ensure_local_token(target)
    assert target.read_bytes() == contents
    assert _token_snapshot(factory) == ([], 0)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_native_lock_rejects_nonregular_files_without_changing_permissions(
    native_credentials, kind
):
    import balancedbooks_launcher as launcher

    path, _ = native_credentials
    lock = path.parent / ".native-setup.lock"
    target = path.parent / "unrelated.txt"
    target.write_text("Keep unrelated permissions")
    os.chmod(target, 0o644)
    if kind == "symlink":
        lock.symlink_to(target)
    else:
        os.mkfifo(lock, 0o640)
    before = lock.lstat().st_mode
    with pytest.raises((OSError, RuntimeError)):
        with launcher._startup_lock(path.parent):
            pytest.fail("Nonregular lock was accepted")
    assert lock.lstat().st_mode == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.read_text() == "Keep unrelated permissions"


@pytest.mark.skipif(
    os.name != "posix", reason="Native setup uses POSIX/macOS file locks"
)
def test_native_settings_creation_is_serialized_across_processes(tmp_path):
    script = """from pathlib import Path
import sys
from balancedbooks_launcher import _startup_lock, _ensure_settings
root = Path(sys.argv[1])
with _startup_lock(root):
    _ensure_settings(root / 'settings.env')
"""
    root = Path(__file__).resolve().parents[1]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    for process in processes:
        output, error = process.communicate(timeout=20)
        assert process.returncode == 0, (output, error)
    from dotenv import dotenv_values

    settings = dotenv_values(tmp_path / "settings.env")
    assert len(settings["SESSION_SECRET_KEY"]) == 64
    assert len(settings["PAYROLL_ENCRYPTION_SECRET"]) == 64
