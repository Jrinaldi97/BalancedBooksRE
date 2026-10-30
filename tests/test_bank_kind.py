"""accounts.bank_kind — which chart accounts are bank or card accounts."""

from app.services.accounting import get_opening_balance_equity_id


def test_seeded_chart_flags_checking_savings_and_the_card(client, seed_accounts):
    rows = client.get("/api/accounts?bank=1").json()
    assert {r["account_number"]: r["bank_kind"] for r in rows} == {
        "1000": "bank",
        "1010": "bank",
        "2100": "credit_card",
    }
    # the flag rides every account response
    one = client.get(f"/api/accounts/{seed_accounts['1000'].id}").json()
    assert one["bank_kind"] == "bank"
    assert (
        client.get(f"/api/accounts/{seed_accounts['1100'].id}").json()["bank_kind"]
        is None
    )


def test_bank_kind_must_match_the_account_type(client, seed_accounts):
    r = client.post(
        "/api/accounts",
        json={
            "name": "Petty Cash",
            "account_number": "1050",
            "account_type": "asset",
            "bank_kind": "bank",
        },
    )
    assert r.status_code == 201 and r.json()["bank_kind"] == "bank"
    r = client.post(
        "/api/accounts",
        json={"name": "Loan", "account_type": "liability", "bank_kind": "bank"},
    )
    assert r.status_code == 400 and "liability" in r.json()["detail"]
    r = client.post(
        "/api/accounts",
        json={"name": "Amex", "account_type": "asset", "bank_kind": "credit_card"},
    )
    assert r.status_code == 400
    # flipping an existing account: the kind must still fit the type
    r = client.put(
        f"/api/accounts/{seed_accounts['1100'].id}", json={"bank_kind": "credit_card"}
    )
    assert r.status_code == 400
    r = client.put(
        f"/api/accounts/{seed_accounts['1100'].id}", json={"bank_kind": "bank"}
    )
    assert r.status_code == 200 and r.json()["bank_kind"] == "bank"
    names = sorted(
        a["name"] for a in client.get("/api/accounts?bank=1&active_only=true").json()
    )
    assert names == [
        "Accounts Receivable",
        "Checking",
        "Credit Card",
        "Petty Cash",
        "Savings",
    ], names


def test_opening_balance_equity_is_created_once(db_session, seed_accounts):
    a = get_opening_balance_equity_id(db_session)
    db_session.commit()
    b = get_opening_balance_equity_id(db_session)
    assert a == b
    from app.models.accounts import Account

    acct = db_session.query(Account).get(a)
    assert (
        acct.account_number == "3900"
        and acct.account_type.value == "equity"
        and acct.is_system
    )


def test_opening_equity_uses_typed_imported_chart(db_session):
    from app.models.accounts import Account, AccountType
    from app.services.safe_errors import DataProblem
    import pytest

    wrong = Account(
        name="Other income", account_number="3900", account_type=AccountType.INCOME
    )
    imported = Account(
        name="Opening Bal Equity",
        account_number="3901",
        account_type=AccountType.EQUITY,
    )
    db_session.add_all([wrong, imported])
    db_session.flush()
    assert get_opening_balance_equity_id(db_session) == imported.id
    imported.name = "Opening Balance Equity"
    imported.account_type = AccountType.INCOME
    db_session.flush()
    with pytest.raises(DataProblem, match="must be an equity"):
        get_opening_balance_equity_id(db_session)
