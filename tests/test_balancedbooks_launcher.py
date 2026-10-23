import json

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
