"""Voiding a journal entry whose line memo is at the column's width.

A split memo is String(300). The reversal prefixes "VOID: " to each line's
memo, so a memo of 295-300 characters came out 301-306 and PostgreSQL
refused the reversal at commit: POST /api/journal/{id}/void answered a bare
500 and the entry could never be voided (a bank-feed journal with a long
Chase description). SQLite does not enforce VARCHAR widths, so these tests
check the length the reversal asks the database to store, and drive the
database refusal itself through a raised DataError."""

from decimal import Decimal

from sqlalchemy.exc import DataError

from app.models.transactions import Transaction
from app.services.accounting import (
    LINE_MEMO_MAX,
    fit_line_memo,
    reversing_lines,
    void_line_memo,
)
from app.services.safe_errors import DataProblem

LONG_MEMO = ("ORIG CO NAME:FIRST AMERICAN TITLE ORIG ID:1234567890 " * 10)[:300]


def _post_long_memo_journal(client, seed_accounts):
    r = client.post(
        "/api/journal",
        json={
            "date": "2026-10-05",
            "description": "Sale of 909 S Lesueur",
            "reference": "feed-105",
            "lines": [
                {
                    "account_id": seed_accounts["1000"].id,
                    "debit": "288000",
                    "description": LONG_MEMO,
                },
                {
                    "account_id": seed_accounts["3000"].id,
                    "credit": "288000",
                    "description": "Sale proceeds",
                },
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _reversal(db_session, entry_id):
    return (
        db_session.query(Transaction)
        .filter(
            Transaction.source_type == "manual_void",
            Transaction.source_id == entry_id,
        )
        .one_or_none()
    )


def test_memo_column_width_is_what_the_cap_uses():
    assert LINE_MEMO_MAX == 300


def test_void_line_memo_fits_the_column():
    assert void_line_memo("Rent") == "VOID: Rent"
    assert void_line_memo(None) == "VOID: "
    capped = void_line_memo(LONG_MEMO)
    assert len(capped) == LINE_MEMO_MAX
    assert capped.startswith("VOID: ORIG CO NAME")
    # Exactly at the edge: 294 + "VOID: " is 300 and is kept whole.
    edge = "x" * (LINE_MEMO_MAX - 6)
    assert void_line_memo(edge) == "VOID: " + edge


def test_reversing_lines_never_exceed_the_memo_width():
    class Line:
        account_id = 1
        debit = Decimal("0")
        credit = Decimal("5")
        description = "y" * 300
        job_id = class_id = cost_code_id = cost_type = function = None

    (out,) = reversing_lines([Line()])
    assert len(out["description"]) <= LINE_MEMO_MAX
    assert out["debit"] == Decimal("5") and out["credit"] == Decimal("0")


def test_void_journal_with_300_char_line_memo_succeeds(
    client, db_session, seed_accounts
):
    je = _post_long_memo_journal(client, seed_accounts)
    r = client.post(f"/api/journal/{je['id']}/void")
    assert r.status_code == 200, r.text

    reversal = _reversal(db_session, je["id"])
    assert reversal is not None
    memos = [ln.description for ln in reversal.lines]
    assert all(len(m) <= LINE_MEMO_MAX for m in memos), [len(m) for m in memos]
    assert all(m.startswith("VOID: ") for m in memos)
    total_dr = sum(ln.debit for ln in reversal.lines)
    total_cr = sum(ln.credit for ln in reversal.lines)
    assert total_dr == total_cr == Decimal("288000")

    # Voiding it again is refused with a 400, not a 500.
    again = client.post(f"/api/journal/{je['id']}/void")
    assert again.status_code == 400


def test_database_refusing_the_reversal_is_a_4xx_and_leaves_the_entry(
    client, db_session, seed_accounts, monkeypatch
):
    je = _post_long_memo_journal(client, seed_accounts)

    def refuse(*args, **kwargs):
        raise DataError(
            "INSERT INTO transaction_lines ...",
            {},
            Exception("value too long for type character varying(300)"),
        )

    monkeypatch.setattr("app.routes.journal.create_journal_entry", refuse)
    r = client.post(f"/api/journal/{je['id']}/void")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail.startswith("This journal entry could not be voided")
    # The SQL statement and its parameters never reach the response.
    assert "INSERT" not in detail
    assert _reversal(db_session, je["id"]) is None

    # The entry is untouched and voids normally once the cause is gone.
    monkeypatch.undo()
    r = client.post(f"/api/journal/{je['id']}/void")
    assert r.status_code == 200, r.text


def test_data_problem_while_voiding_is_a_400_with_its_sentence(
    client, db_session, seed_accounts, monkeypatch
):
    je = _post_long_memo_journal(client, seed_accounts)

    def unbalanced(*args, **kwargs):
        raise DataProblem("Debits and credits must be equal")

    monkeypatch.setattr("app.routes.journal.create_journal_entry", unbalanced)
    r = client.post(f"/api/journal/{je['id']}/void")
    assert r.status_code == 400, r.text
    assert "Debits and credits must be equal" in r.json()["detail"]
    assert _reversal(db_session, je["id"]) is None


def test_fit_line_memo_cuts_only_what_is_too_long():
    assert fit_line_memo(None) is None
    assert fit_line_memo("") == ""
    assert fit_line_memo("short") == "short"
    assert len(fit_line_memo("z" * 1000)) == LINE_MEMO_MAX


def test_database_refusing_a_value_anywhere_is_a_422_not_a_500(
    client, seed_accounts, monkeypatch
):
    """Any route a PostgreSQL value refusal escapes from answers 422 with a
    fixed sentence (the app-wide handler), never a 500 with driver text."""

    def refuse(*args, **kwargs):
        raise DataError(
            "INSERT INTO transaction_lines (description) VALUES (%(d)s)",
            {"d": "secret-parameter"},
            Exception("value too long for type character varying(300)"),
        )

    monkeypatch.setattr("app.routes.journal.create_journal_entry", refuse)
    r = client.post(
        "/api/journal",
        json={
            "date": "2026-10-05",
            "description": "x",
            "lines": [
                {"account_id": seed_accounts["1000"].id, "debit": "1"},
                {"account_id": seed_accounts["3000"].id, "credit": "1"},
            ],
        },
    )
    assert r.status_code == 422, r.text
    assert "nothing was saved" in r.json()["detail"]
    assert "secret-parameter" not in r.text and "INSERT" not in r.text


def test_in_kind_gift_with_a_long_description_posts_within_the_memo_width(
    client, db_session, seed_accounts, seed_customer
):
    client.put("/api/settings", json={"company_type": "nonprofit"})
    client.post("/api/nonprofit/setup-accounts")
    long_desc = ("Steinway model D concert grand, restored, with bench " * 8).strip()
    assert len(long_desc) > LINE_MEMO_MAX
    r = client.post(
        "/api/in-kind-gifts",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-04-20",
            "lines": [
                {
                    "description": long_desc,
                    "quantity": 1,
                    "fair_value": "6500",
                    "debit_account_id": seed_accounts["1000"].id,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    g = r.json()
    # The gift keeps its full description; the posted splits are cut to fit.
    assert g["lines"][0]["description"] == long_desc
    txn = db_session.get(Transaction, g["transaction_id"])
    assert all(len(ln.description or "") <= LINE_MEMO_MAX for ln in txn.lines)
    assert any(ln.description.startswith("In-kind: Steinway") for ln in txn.lines)

    r = client.post(f"/api/in-kind-gifts/{g['id']}/void")
    assert r.status_code == 200, r.text
