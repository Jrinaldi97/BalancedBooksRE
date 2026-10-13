"""Regressions for the 2.9.0 release-gate findings (SlowBooks-Pro-Testing,
reports/2.9.0). Each test names the finding it pins."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.models.accounts import Account, AccountType
from app.models.fixed_assets import FixedAssetType


def _customer(client):
    r = client.post("/api/customers", json={"name": "Gate Donor"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---- R3: unknown fields are a 422, not a silent $0 document ---------------


def test_unknown_field_is_rejected_and_named(client):
    cid = _customer(client)
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "date": "2026-03-01",
            "line_items": [{"description": "x", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 422, r.text
    assert "line_items" in r.text


def test_unknown_nested_field_is_rejected(client):
    cid = _customer(client)
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "date": "2026-03-01",
            "lines": [{"description": "x", "quantity": 1, "rate": 100, "price": 5}],
        },
    )
    assert r.status_code == 422
    assert "price" in r.text


def test_every_request_body_schema_forbids_extras(client):
    """The whole surface, from the spec an agent reads: every object schema
    reachable from a request body declares additionalProperties: false.
    Settings is the one deliberate exception (its key list is data)."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                if name not in seen:
                    seen.add(name)
                    walk(schemas[name])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for ops in spec["paths"].values():
        for op in ops.values():
            if isinstance(op, dict) and op.get("requestBody"):
                walk(op["requestBody"])
    lax = sorted(
        name
        for name in seen
        if schemas[name].get("type") == "object"
        and "properties" in schemas[name]
        and not name.startswith("Body_")
        and name != "SettingsUpdate"
        and schemas[name].get("additionalProperties") is not False
    )
    assert lax == [], f"request models still accept unknown fields: {lax}"


# ---- R4: tax_rate is a fraction, documented, bounded ---------------------


def test_tax_rate_percent_is_rejected_naming_the_unit(client):
    cid = _customer(client)
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "date": "2026-03-01",
            "tax_rate": 8.9,
            "lines": [{"description": "x", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 422, r.text
    assert "fraction" in r.text and "percent" in r.text


def test_tax_rate_fraction_still_books_tax(client, seed_accounts):
    cid = _customer(client)
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "date": "2026-03-01",
            "tax_rate": 0.089,
            "lines": [{"description": "x", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["tax_amount"])) == Decimal("8.90")


@pytest.mark.parametrize(
    "schema",
    [
        "InvoiceCreate",
        "InvoiceUpdate",
        "BillCreate",
        "EstimateCreate",
        "EstimateUpdate",
        "CreditMemoCreate",
        "RecurringCreate",
        "RecurringUpdate",
        "POCreate",
        "POUpdate",
        "SalesReceiptCreate",
    ],
)
def test_tax_rate_unit_is_in_the_spec(client, schema):
    spec = client.get("/openapi.json").json()
    field = spec["components"]["schemas"][schema]["properties"]["tax_rate"]
    text = str(field)
    assert "FRACTION" in text and "default_tax_rate" in text, field
    assert "0.089" in text


def test_settings_default_tax_rate_is_documented_as_percent(client):
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/settings"]["get"]
    assert "percent" in (op.get("description") or "").lower()


# ---- R5: an empty pay run is refused, naming the roster ------------------


def test_empty_pay_run_is_refused(client):
    client.post(
        "/api/employees",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "pay_type": "hourly",
            "pay_rate": 30,
        },
    )
    r = client.post(
        "/api/payroll",
        json={
            "period_start": "2026-03-01",
            "period_end": "2026-03-15",
            "pay_date": "2026-03-20",
            "stubs": [],
        },
    )
    assert r.status_code == 422, r.text
    assert "Ada Lovelace" in r.json()["detail"]
    assert client.get("/api/payroll").json() == []


# ---- R6: PTO enums are in the spec ---------------------------------------


def test_pto_enums_in_spec(client):
    spec = client.get("/openapi.json").json()
    props = spec["components"]["schemas"]["PTOPolicyCreate"]["properties"]
    for name in ("pto_type", "accrual_method"):
        ref = props[name].get("$ref") or props[name].get("allOf", [{}])[0].get("$ref")
        assert ref, props[name]
        assert "enum" in spec["components"]["schemas"][ref.split("/")[-1]]
    r = client.post("/api/pto/policies", json={"name": "Bogus", "pto_type": "nap"})
    assert r.status_code == 422


# ---- R7: DELETE on a posted document names the void route ----------------


def test_delete_invoice_405_names_void(client, seed_accounts):
    cid = _customer(client)
    inv = client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "date": "2026-03-01",
            "lines": [{"description": "x", "quantity": 1, "rate": 100}],
        },
    ).json()
    r = client.delete(f"/api/invoices/{inv['id']}")
    assert r.status_code == 405
    assert r.json()["detail"] == (
        f"Posted documents are voided, not deleted: use POST /api/invoices/{inv['id']}/void"
    )
    assert client.post(f"/api/invoices/{inv['id']}/void").status_code == 200


def test_plain_405_is_unchanged_without_a_void_route(client):
    r = client.delete("/api/reports/profit-loss")
    assert r.status_code == 405
    assert r.json() == {"detail": "Method Not Allowed"}


# ---- R8: depreciation works on a fresh company ---------------------------


def test_fresh_company_has_depreciation_expense_and_a_default_type(
    client, db_session, seed_accounts
):
    from app.seed.chart_of_accounts import CHART_OF_ACCOUNTS

    assert any(a["account_number"] == "6810" for a in CHART_OF_ACCOUNTS)
    types = client.get("/api/fixed-assets/types").json()
    assert [t["name"] for t in types] == ["Equipment"]
    t = types[0]
    assert t["asset_account_id"] and t["accumulated_depreciation_account_id"]
    expense = db_session.get(Account, t["depreciation_expense_account_id"])
    assert expense.name == "Depreciation Expense"
    assert expense.account_type == AccountType.EXPENSE
    # idempotent
    assert len(client.get("/api/fixed-assets/types").json()) == 1
    assert db_session.query(FixedAssetType).count() == 1

    asset = client.post(
        "/api/fixed-assets",
        json={
            "name": "Lathe",
            "asset_type_id": t["id"],
            "purchase_date": "2026-01-01",
            "purchase_price": 6000,
        },
    )
    assert asset.status_code == 201, asset.text
    run = client.post(
        "/api/fixed-assets/run-depreciation", json={"run_date": "2026-03-31"}
    )
    assert run.status_code in (200, 201), run.text


# ---- R9: a missing manifest is logged, not silent ------------------------


def test_missing_manifest_warns_once(monkeypatch, tmp_path, caplog):
    import logging

    from app.services import company_service

    monkeypatch.setenv("SLOWBOOKS_DATA_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr(company_service, "DATABASE_URL", "sqlite:///x.db")
    monkeypatch.setattr(company_service, "_warned_missing_manifest", False)
    with caplog.at_level(logging.WARNING, logger="app.services.company_service"):
        msg = company_service.warn_if_manifest_missing()
        assert msg and "nowhere" in msg and "SLOWBOOKS_DATA_DIR" in msg
        assert company_service.manifest_list_companies() == []
    assert sum("Company manifest not found" in r.message for r in caplog.records) == 1


# ---- R2/R2b: Save PDF attempts the write and explains a refusal ----------


def test_save_report_falls_back_when_documents_refuses(monkeypatch, tmp_path):
    import desktop_launcher

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    data = tmp_path / "data"
    monkeypatch.setattr(desktop_launcher.Path, "home", lambda: home)
    monkeypatch.setattr(desktop_launcher, "get_data_dir", lambda: data)
    monkeypatch.setattr(desktop_launcher.sys, "platform", "darwin")

    real = desktop_launcher._write_unique

    def refusing(folder, name, payload):
        if str(folder).startswith(str(home / "Documents")):
            raise PermissionError(1, "Operation not permitted")
        return real(folder, name, payload)

    monkeypatch.setattr(desktop_launcher, "_write_unique", refusing)
    dest, note = desktop_launcher._save_report("r.pdf", b"%PDF-")
    assert dest == data / "Reports" / "r.pdf" and dest.read_bytes() == b"%PDF-"
    assert "was not allowed to write to" in note
    assert str(home / "Documents" / "SlowBooks Pro" / "Reports") in note
    assert "Files and Folders" in note

    # the reveal bridge accepts the fallback folder too
    api = desktop_launcher.PickerApi(3001)
    monkeypatch.setattr(desktop_launcher.subprocess, "Popen", lambda *a, **k: None)
    assert api.reveal_path(str(dest)) == {"success": True}


def test_save_report_prefers_documents(monkeypatch, tmp_path):
    import desktop_launcher

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(desktop_launcher.Path, "home", lambda: home)
    monkeypatch.setattr(desktop_launcher.sys, "platform", "linux")
    dest, note = desktop_launcher._save_report("r.pdf", b"a")
    again, _ = desktop_launcher._save_report("r.pdf", b"b")
    assert note is None
    assert dest == home / "Documents" / "SlowBooks Pro" / "Reports" / "r.pdf"
    assert again.name == "r (2).pdf"


def test_backup_permission_error_names_folder_and_backup(monkeypatch, tmp_path):
    import shutil

    import desktop_launcher
    from app.services import backup_service

    home = tmp_path / "home"
    home.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "slowbooks_20260101_000000.db").write_bytes(b"x")
    monkeypatch.setattr(desktop_launcher.Path, "home", lambda: home)
    monkeypatch.setattr(backup_service, "BACKUP_DIR", backups)
    monkeypatch.setattr(desktop_launcher.sys, "platform", "darwin")

    def refuse(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(shutil, "copy2", refuse)
    r = desktop_launcher.PickerApi(3001).save_backup_file(
        "slowbooks_20260101_000000.db"
    )
    assert r["success"] is False
    assert str(home / "Downloads") in r["error"]
    assert "slowbooks_20260101_000000.db" in r["error"]
    assert "Files and Folders" in r["error"]


def test_mac_bundle_declares_folder_usage_strings():
    spec = open("packaging/macos/SlowBooksPro-mac.spec").read()
    assert "NSDocumentsFolderUsageDescription" in spec
    assert "NSDownloadsFolderUsageDescription" in spec


def test_release_staples_the_app_before_the_dmg():
    src = open("packaging/macos/release.py").read()
    app_staple = src.index('_notarize(notary_zip, notary_profile, report_dir, "app")')
    assert src.index("_staple(app, report_dir)") > app_staple
    assert src.index('stage = work_dir / "dmg-stage"') > src.index(
        "_staple(app, report_dir)"
    )
    assert "_verify_dmg_contents_stapled(final_dmg" in src


def test_openapi_documents_void_over_delete(client):
    spec = client.get("/openapi.json").json()
    assert "void" in spec["info"]["description"].lower()


# ---- Round 3: the macOS bridge, silent branches, CSV, identity ----------


def test_csp_allows_eval_only_under_the_desktop_launcher():
    """pywebview builds window.pywebview.api with `new Function`; WebKit
    enforces the page CSP on it. The desktop shell needs 'unsafe-eval'; a
    browser install must not get it."""
    from app.main import _build_csp

    assert "'unsafe-eval'" in _build_csp(desktop=True)
    assert "'unsafe-eval'" not in _build_csp(desktop=False)
    assert _build_csp(desktop=True).replace(" 'unsafe-eval'", "") == _build_csp(
        desktop=False
    )


def test_csp_header_follows_the_desktop_flag(client):
    r = client.get("/api/auth/status")
    assert "script-src 'self' 'unsafe-inline'" in r.headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in r.headers["Content-Security-Policy"]


def test_csp_relaxed_only_for_loopback_even_with_the_desktop_flag(monkeypatch):
    """Keith, #98: the launcher sets SLOWBOOKS_DESKTOP=1 for --serve-lan too,
    so the flag alone handed 'unsafe-eval' to every LAN browser. The web
    view only ever connects from loopback; a LAN client must get strict."""
    import app.main as m
    from fastapi.testclient import TestClient

    monkeypatch.setattr(m, "_DESKTOP_FLAG", True)
    loopback = TestClient(m.app, client=("127.0.0.1", 40000)).get("/static/js/app.js")
    assert "'unsafe-eval'" in loopback.headers["Content-Security-Policy"]
    lan = TestClient(m.app, client=("192.168.68.60", 40000)).get("/static/js/app.js")
    assert "'unsafe-eval'" not in lan.headers["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-inline'" in lan.headers["Content-Security-Policy"]
    # and without the flag even loopback is strict (Docker / browser install)
    monkeypatch.setattr(m, "_DESKTOP_FLAG", False)
    r = TestClient(m.app, client=("127.0.0.1", 40000)).get("/static/js/app.js")
    assert "'unsafe-eval'" not in r.headers["Content-Security-Policy"]


def test_is_loopback():
    from app.main import _is_loopback

    assert all(_is_loopback(h) for h in ("127.0.0.1", "127.1.2.3", "::1", "localhost"))
    assert not any(
        _is_loopback(h) for h in ("192.168.68.60", "10.0.0.8", "testclient", "", None)
    )


def test_desktop_fetches_get_inline_not_attachment(client, seed_accounts):
    """A desktop-shell fetch() never receives Content-Disposition: attachment
    (both webviews swallow those as native downloads). Every CSV producer,
    not just app/routes/csv.py, is covered because the middleware does it."""
    url = "/api/reports/statement-of-activities/csv?start_date=2026-01-01&end_date=2026-12-31"
    plain = client.get(url)
    assert plain.status_code == 200, plain.text
    assert plain.headers["Content-Disposition"].startswith("attachment")
    desktop = client.get(url, headers={"X-Slowbooks-Desktop": "1"})
    assert desktop.headers["Content-Disposition"].startswith("inline; filename=")
    assert desktop.text == plain.text


def test_shim_never_returns_in_silence_when_the_bridge_is_missing():
    src = open("app/static/js/desktop_shim.js", encoding="utf-8").read()
    assert src.count("bridgeMissing(") >= 3  # pdf, html, liveness check
    assert "pywebviewready" in src and "checkBridge" in src
    assert "save_document_file" in src


def test_save_document_file_writes_like_save_pdf(monkeypatch, tmp_path):
    import base64

    import desktop_launcher

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(desktop_launcher.Path, "home", lambda: home)
    monkeypatch.setattr(desktop_launcher.sys, "platform", "linux")
    api = desktop_launcher.PickerApi(3001)
    r = api.save_document_file(
        "statement-of-activities_2026.csv", base64.b64encode(b"a,b\n1,2\n").decode()
    )
    assert r["success"] is True
    dest = (
        home
        / "Documents"
        / "SlowBooks Pro"
        / "Reports"
        / "statement-of-activities_2026.csv"
    )
    assert r["path"] == str(dest) and dest.read_bytes() == b"a,b\n1,2\n"


def test_installer_clears_internal_on_upgrade():
    iss = open("packaging/windows/SlowBooksPro.iss").read()
    assert "[InstallDelete]" in iss
    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in iss
    assert iss.index("[InstallDelete]") < iss.index("[Files]")


def test_auth_status_names_the_company_a_setup_would_rename(unauthed_client):

    r = unauthed_client.get("/api/auth/status").json()
    assert (
        r["setup_needed"] is True and r["company_name"] == "" and r["has_data"] is False
    )


def test_setup_and_settings_keep_the_manifest_name_in_step(tmp_path, monkeypatch):
    import json

    from app.services import company_service
    from app.services.company_service import sync_manifest_name

    data = tmp_path / "data"
    monkeypatch.setenv("SLOWBOOKS_DATA_DIR", str(data))
    monkeypatch.setattr(
        company_service,
        "DATABASE_URL",
        "sqlite:///" + str(data / "companies" / "riverbend.db"),
    )
    company_service._write_manifest(
        {
            "companies": [{"name": "Riverbend Community Arts", "file": "riverbend.db"}],
            "last_opened": "riverbend.db",
        }
    )
    assert sync_manifest_name("NEONpulse Techshop") is True
    manifest = json.loads((data / "companies.json").read_text(encoding="utf-8"))
    assert manifest["companies"][0]["name"] == "NEONpulse Techshop"
    assert sync_manifest_name("NEONpulse Techshop") is False  # idempotent
    assert sync_manifest_name("") is False  # blank never renames


def test_settings_company_name_updates_manifest(client, tmp_path, monkeypatch):
    import json

    from app.services import company_service

    data = tmp_path / "data"
    monkeypatch.setenv("SLOWBOOKS_DATA_DIR", str(data))
    monkeypatch.setattr(
        company_service,
        "DATABASE_URL",
        "sqlite:///" + str(data / "companies" / "acme.db"),
    )
    company_service._write_manifest(
        {
            "companies": [{"name": "Old Name", "file": "acme.db"}],
            "last_opened": "acme.db",
        }
    )
    r = client.put("/api/settings", json={"company_name": "New Name LLC"})
    assert r.status_code == 200, r.text
    assert (
        json.loads((data / "companies.json").read_text(encoding="utf-8"))["companies"][
            0
        ]["name"]
        == "New Name LLC"
    )
    # and the list reconciles from settings even if the manifest is edited by hand
    company_service._write_manifest(
        {
            "companies": [{"name": "Hand Edited", "file": "acme.db"}],
            "last_opened": "acme.db",
        }
    )
    assert client.get("/api/companies").json()[0]["name"] == "New Name LLC"


# ---- Round 4: reconciliation must never create two files with one name ----


def _two_company_manifest(tmp_path, monkeypatch, current="qa-host.db"):
    from app.services import company_service

    data = tmp_path / "data"
    monkeypatch.setenv("SLOWBOOKS_DATA_DIR", str(data))
    monkeypatch.setattr(
        company_service,
        "DATABASE_URL",
        "sqlite:///" + str(data / "companies" / current),
    )
    company_service._write_manifest(
        {
            "companies": [
                {"name": "QA Host", "file": "qa-host.db"},
                {"name": "NEONpulse Techshop", "file": "neonpulse-techshop.db"},
            ],
            "last_opened": current,
        }
    )
    return data


def test_sync_refuses_a_name_another_file_already_uses(tmp_path, monkeypatch, caplog):
    import json
    import logging

    from app.services.company_service import sync_manifest_name

    data = _two_company_manifest(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="app.services.company_service"):
        assert sync_manifest_name("NEONpulse Techshop") is False
        assert sync_manifest_name("neonpulse techshop") is False  # case-insensitive
    names = [
        c["name"]
        for c in json.loads((data / "companies.json").read_text(encoding="utf-8"))[
            "companies"
        ]
    ]
    assert names == ["QA Host", "NEONpulse Techshop"]
    assert "already uses that name" in caplog.text
    # its own current name, or a genuinely new one, is fine
    assert sync_manifest_name("QA Host") is False
    assert sync_manifest_name("QA Host 2") is True


def test_settings_rename_to_another_files_name_is_409(client, tmp_path, monkeypatch):
    import json

    data = _two_company_manifest(tmp_path, monkeypatch)
    r = client.put("/api/settings", json={"company_name": "NEONpulse Techshop"})
    assert r.status_code == 409, r.text
    assert "neonpulse-techshop.db" in r.json()["detail"]
    assert client.get("/api/settings").json()["company_name"] != "NEONpulse Techshop"
    names = [
        c["name"]
        for c in json.loads((data / "companies.json").read_text(encoding="utf-8"))[
            "companies"
        ]
    ]
    assert names == ["QA Host", "NEONpulse Techshop"]
    # renaming to something unique still works and follows through
    assert (
        client.put("/api/settings", json={"company_name": "QA Host Books"}).status_code
        == 200
    )
    names = [
        c["name"]
        for c in json.loads((data / "companies.json").read_text(encoding="utf-8"))[
            "companies"
        ]
    ]
    assert names == ["QA Host Books", "NEONpulse Techshop"]


def test_setup_with_another_files_name_is_409_and_writes_nothing(
    unauthed_client, tmp_path, monkeypatch
):
    _two_company_manifest(tmp_path, monkeypatch)
    r = unauthed_client.post(
        "/api/auth/setup",
        json={
            "operator_name": "T",
            "operator_email": "t@example.com",
            "company_name": "NEONpulse Techshop",
            "password": "test-password-123",
        },
    )
    assert r.status_code == 409, r.text
    assert unauthed_client.get("/api/auth/status").json()["setup_needed"] is True


# ---- Linux gate: the documented Docker install must migrate from empty ----


def _migrate_fresh_sqlite(tmp_path):
    from alembic import command
    from alembic.config import Config

    db = tmp_path / "fresh.db"
    url = "sqlite:///" + db.as_posix()
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    return db


def test_migrations_alone_create_every_table_a_foreign_key_references(tmp_path):
    """Postgres enforces a foreign key at CREATE TABLE time; SQLite does not.
    So a migration referencing a table that only create_all() makes passes
    every desktop install and kills `docker compose up` (2.9.0 Linux gate:
    user_preferences -> users, broken since v2.8.0). Walk the schema
    alembic alone produced and demand every REFERENCES target exists."""
    import re
    import sqlite3

    db = _migrate_fresh_sqlite(tmp_path)
    con = sqlite3.connect(db)
    tables = {
        r[0] for r in con.execute("select name from sqlite_master where type='table'")
    }
    assert "users" in tables and "user_preferences" in tables
    dangling = []
    for name, sql in con.execute(
        "select name, sql from sqlite_master where type='table'"
    ):
        for ref in re.findall(r"REFERENCES\s+\"?(\w+)\"?", sql or ""):
            if ref not in tables:
                dangling.append(f"{name} -> {ref}")
    assert dangling == [], dangling


def test_users_migration_is_idempotent_on_a_file_that_already_has_the_table(tmp_path):
    """A desktop file that got users from create_all() and is already past
    b2c3 never runs a0b1; but if it ever does (a downgrade/upgrade cycle),
    it must not fail on an existing table."""
    import sqlite3

    from alembic import command
    from alembic.config import Config

    db = tmp_path / "pre.db"
    con = sqlite3.connect(db)
    con.execute("create table users (id integer primary key, username varchar(100))")
    con.commit()
    con.close()
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = "sqlite:///" + db.as_posix()
    command.upgrade(cfg, "head")  # must not raise on the pre-existing users table


def test_postgres_company_list_reports_is_current(monkeypatch, db_session):
    from app.services import company_service

    monkeypatch.setattr(
        company_service,
        "DATABASE_URL",
        "postgresql://u:p@host:5432/bookkeeper?sslmode=disable",
    )
    rows = company_service.list_companies(db_session)
    current = [r for r in rows if r["is_current"]]
    assert len(current) == 1 and current[0]["database_name"] == "bookkeeper"


# ---- Linux gate: `docker compose up` must start as documented -------------


def _prod_postgres(monkeypatch, url="postgresql://u:p@postgres:5432/db"):
    import app.config as cfg

    monkeypatch.setattr(cfg, "DATABASE_URL", url)
    monkeypatch.setattr(
        cfg, "PAYROLL_ENCRYPTION_SECRET", "a-real-secret-not-the-dev-one"
    )
    monkeypatch.setattr(cfg, "APP_DEBUG", False)


def test_production_guard_refuses_plaintext_db_without_the_flag(monkeypatch):
    import app.main as m

    _prod_postgres(monkeypatch)
    monkeypatch.delenv("SLOWBOOKS_PRIVATE_NETWORK", raising=False)
    with pytest.raises(RuntimeError, match="TLS mode"):
        m._run_startup_security_checks()


def test_private_network_flag_relaxes_only_the_transport_guards(monkeypatch, caplog):
    import logging

    import app.main as m

    _prod_postgres(monkeypatch)
    monkeypatch.setattr(m, "_create_missing_tables", lambda: None)  # isolate transport policy from schema bootstrap
    monkeypatch.setenv("SLOWBOOKS_PRIVATE_NETWORK", "1")
    monkeypatch.setattr(m, "FORCE_HTTPS", False)
    monkeypatch.setattr(m.Base.metadata, "create_all", lambda **kw: None)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        m._run_startup_security_checks()  # the compose case: plain http, bridge-network db
    assert "SLOWBOOKS_PRIVATE_NETWORK=1" in caplog.text

    # the encryption-key guards are never relaxed
    import app.config as cfg

    monkeypatch.setattr(
        cfg, "PAYROLL_ENCRYPTION_SECRET", "slowbooks-dev-payroll-key-change-me"
    )
    with pytest.raises(RuntimeError, match="dev default"):
        m._run_startup_security_checks()


def test_compose_file_declares_the_single_host_flag():
    import yaml

    compose = yaml.safe_load(open("docker-compose.yml"))
    env = compose["services"]["slowbooks"]["environment"]
    assert env["SLOWBOOKS_PRIVATE_NETWORK"].startswith(
        "${SLOWBOOKS_PRIVATE_NETWORK:-1}"
    )
    assert env["FORCE_HTTPS"].startswith("${FORCE_HTTPS:-false}")


def test_create_all_is_serialized_under_postgres(monkeypatch):
    """Two uvicorn workers must not race create_all on a fresh Postgres."""
    from contextlib import contextmanager

    import app.main as m

    calls = []

    class FakeConn:
        def execute(self, stmt):
            calls.append(("execute", str(stmt)))

    class FakeEngine:
        class dialect:
            name = "postgresql"

        @contextmanager
        def begin(self):
            calls.append(("begin",))
            yield FakeConn()
            calls.append(("commit",))

    monkeypatch.setattr(m, "engine", FakeEngine())
    monkeypatch.setattr(
        m.Base.metadata,
        "create_all",
        lambda bind=None: calls.append(("create_all", type(bind).__name__)),
    )
    m._create_missing_tables()
    assert calls[0] == ("begin",)
    assert "pg_advisory_xact_lock" in calls[1][1]
    assert calls[2] == ("create_all", "FakeConn")
    assert calls[-1] == ("commit",)


# ---- 2.9.1: post-release tidy ------------------------------------------


def test_migrations_create_every_model_table(tmp_path):
    import sqlite3

    from app.database import Base
    import app.models  # noqa: F401

    db = _migrate_fresh_sqlite(tmp_path)
    migrated = {
        r[0]
        for r in sqlite3.connect(db).execute(
            "select name from sqlite_master where type='table'"
        )
    }
    missing = sorted(set(Base.metadata.tables) - migrated)
    assert missing == [], f"tables only create_all() makes: {missing}"


def test_ai_api_key_can_be_cleared_with_an_explicit_empty_string(client):
    base = {"provider": "openai", "model": "gpt-5.4-mini"}
    r = client.put(
        "/api/analytics/ai-config", json={**base, "api_key": "sk-test-1234567890"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_api_key"] is True
    # omitted -> kept
    r = client.put("/api/analytics/ai-config", json=base)
    assert r.json()["has_api_key"] is True
    # explicit empty string -> cleared
    r = client.put("/api/analytics/ai-config", json={**base, "api_key": ""})
    assert r.status_code == 200, r.text
    assert r.json()["has_api_key"] is False


def test_settings_page_never_round_trips_a_blank_ai_key():
    src = open("app/static/js/settings.js", encoding="utf-8").read()
    assert "keyPayload" in src and "ai-settings-key-remove" in src
    assert "api_key: document.getElementById('ai-settings-key').value," not in src


def test_nonprofit_vocabulary_on_analytics_aging_and_donor_card():
    analytics = open("app/static/js/analytics.js", encoding="utf-8").read()
    assert '_agingTable(data.ar_aging, T("Customer"))' in analytics
    reports = open("app/static/js/reports.js", encoding="utf-8").read()
    assert "Sales totals per donor" not in reports
    assert "Contribution totals per donor" in reports


def test_windows_workflow_publishes_checksums():
    wf = open(".github/workflows/windows.yml").read()
    assert "SHA256SUMS.windows" in wf
    assert wf.count("SHA256SUMS.windows") >= 3  # written, uploaded, attached
