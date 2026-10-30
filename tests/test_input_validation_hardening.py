"""Hostile-scalar hardening, through real endpoints.

A TestClient audit found four classes of malformed input that passed schema
validation and blew up downstream as an uncaught 500:

  1. A non-finite float (NaN / Infinity) in any float field — a bill line
     quantity/rate hit decimal.InvalidOperation, a bill-payment amount
     became a NULL insert (IntegrityError).
  2. A NUL byte (\\x00) in any string — stored silently on SQLite, a 500
     on PostgreSQL.
  3. A document date at the edges of the date type — an invoice dated
     9999-12-31 overflowed computing its Net-30 due date.
  4. A PUT that explicitly sets a NOT NULL column to null — IntegrityError.

The fix is central (app/schemas/common.StrictModel and a routes helper), so
these pin a representative field for each class to a 422, not a 500.
"""

import json

# JSON has no NaN/Infinity literal, so httpx refuses to serialize one. A real
# client sends them as the non-standard bare tokens Python's json module emits
# (and accepts) — post the raw body the way a hostile caller would.
_JSON = {"Content-Type": "application/json"}


def _raw(client, method, url, payload):
    body = json.dumps(payload)  # allow_nan=True: writes NaN / Infinity
    return client.request(method, url, content=body, headers=_JSON)


def _vendor(db_session, name="V"):
    from app.models.accounts import Account
    from app.models.contacts import Vendor

    expense = db_session.query(Account).filter_by(account_number="6000").first()
    v = Vendor(
        name=name,
        is_active=True,
        default_expense_account_id=expense.id if expense else None,
    )
    db_session.add(v)
    db_session.commit()
    return v


# ---------------------------------------------------------------------------
# 1. Non-finite floats
# ---------------------------------------------------------------------------


def test_bill_line_quantity_nan_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = _raw(
        client,
        "POST",
        "/api/bills",
        {
            "vendor_id": vendor.id,
            "bill_number": "B-NAN",
            "date": "2026-05-01",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": float("nan"), "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


def test_bill_line_rate_infinity_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = _raw(
        client,
        "POST",
        "/api/bills",
        {
            "vendor_id": vendor.id,
            "bill_number": "B-INF",
            "date": "2026-05-01",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": float("inf")}],
        },
    )
    assert r.status_code == 422, r.text


def test_bill_payment_amount_nan_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = _raw(
        client,
        "POST",
        "/api/bill-payments",
        {"vendor_id": vendor.id, "date": "2026-05-01", "amount": float("nan")},
    )
    assert r.status_code == 422, r.text


def test_credit_memo_line_rate_nan_is_422(
    client, db_session, seed_accounts, seed_customer
):
    r = _raw(
        client,
        "POST",
        "/api/credit-memos",
        {
            "customer_id": seed_customer.id,
            "date": "2026-05-01",
            "lines": [{"description": "x", "quantity": 1, "rate": float("nan")}],
        },
    )
    assert r.status_code == 422, r.text


def test_benefit_rate_nan_is_422(client, db_session, seed_accounts):
    r = _raw(
        client,
        "POST",
        "/api/benefits/codes",
        {
            "code": "DENTAL",
            "name": "Dental",
            "rate": {"employee_rate": float("nan"), "employer_rate": 0},
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 2. NUL byte in strings
# ---------------------------------------------------------------------------


def test_customer_name_with_nul_byte_is_422(client, db_session):
    r = client.post("/api/customers", json={"name": "Nul\x00Co"})
    assert r.status_code == 422, r.text


def test_bill_notes_with_nul_byte_is_422(client, db_session, seed_accounts):
    vendor = _vendor(db_session)
    r = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor.id,
            "bill_number": "B-NUL",
            "date": "2026-05-01",
            "tax_rate": 0,
            "notes": "a\x00b",
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 3. Out-of-range dates
# ---------------------------------------------------------------------------


def test_invoice_far_future_date_is_422_not_500(
    client, db_session, seed_accounts, seed_customer
):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "9999-12-31",
            "terms": "Net 30",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


def test_invoice_year_one_date_is_422(client, db_session, seed_accounts, seed_customer):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "0001-01-01",
            "terms": "Net 30",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 422, r.text


def test_invoice_ordinary_date_still_accepted(
    client, db_session, seed_accounts, seed_customer
):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-05-01",
            "terms": "Net 30",
            "tax_rate": 0,
            "lines": [{"description": "x", "quantity": 1, "rate": 1}],
        },
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# 4. Explicit null on a NOT NULL column
# ---------------------------------------------------------------------------


def test_put_customer_name_null_is_422(client, db_session, seed_customer):
    r = client.put(f"/api/customers/{seed_customer.id}", json={"name": None})
    assert r.status_code == 422, r.text
    # The non-null name survived.
    assert (
        client.get(f"/api/customers/{seed_customer.id}").json()["name"]
        == "Test Customer"
    )


def test_put_vendor_name_null_is_422(client, db_session):
    vendor = _vendor(db_session, name="Acme")
    r = client.put(f"/api/vendors/{vendor.id}", json={"name": None})
    assert r.status_code == 422, r.text


def test_put_customer_empty_body_stays_noop_200(client, db_session, seed_customer):
    r = client.put(f"/api/customers/{seed_customer.id}", json={})
    assert r.status_code == 200, r.text
