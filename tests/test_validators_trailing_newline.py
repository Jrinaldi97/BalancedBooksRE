"""Input validators must reject a trailing newline.

In Python's ``re`` a ``$`` also matches just *before* a final ``\\n``, so a
pattern like ``^[a-z]+$`` used with ``.match()``/``.search()`` happily accepts
``"value\\n"``. Every validator that guards external input now uses
``re.fullmatch`` (anchored to the whole string, no ``^``/``$``), which rejects
the trailing newline -- and ``"\\r\\n"`` -- while still accepting the value.

Some HTTP entry points ``strip()`` their input before the regex (the account
form schema, the username route, the idempotency-key header, the AI settings
route), so the bug was not reachable there in practice; those cases are locked
at the compiled-pattern level below. The validators that do *not* pre-strip
(backup / company filenames, preference key) are exercised through their
public validation function, where the newline would otherwise slip through.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routes.accounts import _check_account_number
from app.routes.preferences import _check_key
from app.routes.users import _USERNAME_RE
from app.services.ai_service import CLOUDFLARE_ACCOUNT_ID_RE
from app.services.backup_service import _safe_backup_filename
from app.services.company_service import (
    _VALID_DB_NAME,
    create_company,
    safe_company_filename,
)

# --- validators reached with un-stripped input (behavioural red -> green) ---


@pytest.mark.parametrize("suffix", ["\n", "\r\n"])
def test_backup_filename_rejects_trailing_newline(suffix):
    assert _safe_backup_filename("acme_20250101_120000.db" + suffix) is None


def test_backup_filename_accepts_valid():
    name = "acme_20250101_120000.db"
    assert _safe_backup_filename(name) == name


@pytest.mark.parametrize("suffix", ["\n", "\r\n"])
def test_company_filename_rejects_trailing_newline(suffix):
    assert safe_company_filename("acme.db" + suffix) is None


def test_company_filename_accepts_valid():
    assert safe_company_filename("acme.db") == "acme.db"


@pytest.mark.parametrize("suffix", ["\n", "\r\n"])
def test_create_company_rejects_db_name_with_trailing_newline(suffix):
    db = MagicMock()
    # No existing company, so without the fix the newline name sails past the
    # allow-list and the service goes on to build an engine / run DDL.
    db.query.return_value.filter.return_value.first.return_value = None
    # _init_company_db is patched too: without the fix the newline name reaches
    # it as "sqlite:///acme\n" and a real database file gets written to disk.
    with patch("app.services.company_service._is_sqlite", return_value=False), patch(
        "app.services.company_service.create_engine"
    ) as engine_factory, patch(
        "app.services.company_service._init_company_db"
    ) as init_db:
        result = create_company(db, "Acme", "acme" + suffix)
    assert result["success"] is False
    assert "Invalid database name" in result["error"]
    engine_factory.assert_not_called()
    init_db.assert_not_called()


@pytest.mark.parametrize("suffix", ["\n", "\r\n"])
def test_account_number_rejects_trailing_newline(suffix):
    with pytest.raises(HTTPException) as exc:
        _check_account_number("6150" + suffix)
    assert exc.value.status_code == 400


def test_account_number_accepts_valid():
    assert _check_account_number("6150") is None


@pytest.mark.parametrize("suffix", ["\n", "\r\n"])
def test_preference_key_rejects_trailing_newline(suffix):
    with pytest.raises(HTTPException) as exc:
        _check_key("theme" + suffix)
    assert exc.value.status_code == 422


def test_preference_key_accepts_valid():
    assert _check_key("theme") is None


# --- validators whose HTTP entry strips first (lock the compiled pattern) ---
#
# These are validated inline and every HTTP caller strip()s the value before
# the regex, so a behavioural "rejects value\n" test is impossible (the strip
# turns it back into the valid value). Guard against regression to the Python
# ``$`` footgun instead: the pattern must be anchor-free so .match() can never
# be used as an accidental partial/trailing-newline-tolerant validator.
@pytest.mark.parametrize(
    "rx, valid",
    [
        (_USERNAME_RE, "alice"),
        (CLOUDFLARE_ACCOUNT_ID_RE, "0123456789abcdef0123456789abcdef"),
    ],
)
def test_inline_validator_pattern_is_anchor_free(rx, valid):
    assert "$" not in rx.pattern and "^" not in rx.pattern
    assert rx.fullmatch(valid) is not None
    assert rx.fullmatch(valid + "\n") is None
    assert rx.fullmatch(valid + "\r\n") is None


def test_valid_db_name_pattern_is_anchor_free():
    assert "$" not in _VALID_DB_NAME.pattern and "^" not in _VALID_DB_NAME.pattern
    assert _VALID_DB_NAME.fullmatch("acme") is not None
    assert _VALID_DB_NAME.fullmatch("acme\n") is None
