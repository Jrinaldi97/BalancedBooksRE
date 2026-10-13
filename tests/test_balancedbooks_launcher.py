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
