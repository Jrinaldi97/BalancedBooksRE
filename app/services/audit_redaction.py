"""What the audit log never shows: password hashes and secrets (2.24.0 gate,
NEW-148).

The audit log snapshots a row's values before and after a change. A user's
``password_hash`` was redacted on INSERT and DELETE but copied on UPDATE,
and a settings row was snapshotted with its value, so the argon2id hash of
the operator's password, the company's session id and the Fernet
ciphertext of the closing-date password were in the log, readable by the
Accountant role, carried in the Accountant's Package and in the
Accountant's Copy. This module is the one list of what is redacted, and
every reader and writer of audit values goes through it: the flush hook
(what is written), ``GET /api/audit`` (what is read, so rows written
before 2.24.0 are redacted too), the package's audit CSV, the Closing Date
Exceptions report, and the copy (whose audit_log is rewritten through it).
"""

from __future__ import annotations

from typing import Any

REDACTED = "***"

# Columns, on any table, whose value is a secret or a digest of one.
REDACT_COLUMNS = frozenset(
    {
        "password_hash",
        "token_hash",
        "token_enc",
        "secret",
        "api_key",
        "api_key_enc",
        "routing_number_enc",
        "account_number_enc",
        "webhook_secret",
        "signing_secret",
    }
)
_REDACT_SUFFIXES = ("_enc", "_secret", "_token", "_password")


def _sensitive_setting_keys() -> frozenset:
    KEY_TOKEN = "accountants_copy_token"  # Reserved upstream integration secret.
    from app.services.settings_service import _SENSITIVE_KEYS, ENCRYPTED_SETTINGS_KEYS

    return frozenset(
        {
            *_SENSITIVE_KEYS,
            *ENCRYPTED_SETTINGS_KEYS,
            KEY_TOKEN,
            "ai_api_key",
            "closing_date_password",
        }
    )


_SETTING_KEYS: frozenset | None = None


def sensitive_setting_keys() -> frozenset:
    global _SETTING_KEYS
    if _SETTING_KEYS is None:
        _SETTING_KEYS = _sensitive_setting_keys()
    return _SETTING_KEYS


_NOT_SECRETS = frozenset({"must_change_password", "ask_password_on_start"})


def is_secret_column(name: str) -> bool:
    if name in _NOT_SECRETS:
        return False
    return name in REDACT_COLUMNS or name.endswith(_REDACT_SUFFIXES)


def redact_values(table_name: str, values: dict | None) -> dict | None:
    """A copy of a row's snapshot with its secrets replaced by ***: every
    secret column, and a settings row's value when its key is one of the
    secrets."""
    if not isinstance(values, dict):
        return values
    out: dict[str, Any] = {}
    secret_setting = table_name == "settings" and (
        "key" not in values or values.get("key") in sensitive_setting_keys()
    )
    for k, v in values.items():
        # a secret is text; a flag (must_change_password, a true/false) says
        # something happened and stays (2.24.0 gate, W-26 / NEW-166)
        if is_secret_column(k) and isinstance(v, str) and v != "":
            out[k] = REDACTED
        elif secret_setting and k == "value" and isinstance(v, str) and v != "":
            out[k] = REDACTED
        else:
            out[k] = v
    return out


def redact_row(row) -> dict:
    """An AuditLog row as the API answers it, redacted."""
    return {
        "id": row.id,
        "table_name": row.table_name,
        "record_id": row.record_id,
        "action": row.action,
        "old_values": redact_values(row.table_name, row.old_values),
        "new_values": redact_values(row.table_name, row.new_values),
        "changed_fields": row.changed_fields,
        "timestamp": row.timestamp,
        "source": row.source,
        "username": row.username,
    }
