"""Line replacement on a posted document edit is audited per line (#289).

Editing a bill's or an invoice's lines, and re-posting its journal,
removed the old lines with Query.delete(), which never passes through
session.deleted, so the after_flush hook saw nothing: the edit's UPDATE
and the new lines' INSERTs were in the log, the old lines' amounts were
not. Every line replacement goes through audited_bulk_delete now, so the
log carries a DELETE event for each old bill line, invoice line and
journal line, with its account and amounts, beside the INSERTs of the
new ones.
"""


def _events(client, table, record_id=None):
    params = {"table_name": table, "limit": 200}
    if record_id is not None:
        params["record_id"] = record_id
    rows = client.get("/api/audit", params=params).json()
    return (
        rows if isinstance(rows, list) else rows.get("items", rows.get("entries", []))
    )


def test_a_bill_edit_leaves_each_old_line_and_journal_line_in_the_log(
    client, seed_accounts
):
    vendor = client.post("/api/vendors", json={"name": "Pier Lumber"}).json()
    acct = seed_accounts["6000"].id
    bill = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor["id"],
            "date": "2026-09-10",
            "lines": [
                {
                    "description": "Lumber",
                    "account_id": acct,
                    "quantity": 2,
                    "rate": "150.00",
                    "line_order": 0,
                },
                {
                    "description": "Nails",
                    "account_id": acct,
                    "quantity": 1,
                    "rate": "20.00",
                    "line_order": 1,
                },
            ],
        },
    )
    assert bill.status_code == 201, bill.text
    bill = bill.json()
    old_line_ids = {ln["id"] for ln in bill["lines"]}
    assert len(old_line_ids) == 2
    r = client.put(
        f"/api/bills/{bill['id']}",
        json={
            "lines": [
                {
                    "description": "Lumber",
                    "account_id": acct,
                    "quantity": 3,
                    "rate": "150.00",
                    "line_order": 0,
                }
            ]
        },
    )
    assert r.status_code == 200, r.text

    deletes = [
        e for e in _events(client, "bill_lines") if e["action"].upper() == "DELETE"
    ]
    assert {e["record_id"] for e in deletes} == old_line_ids
    by_desc = {e["old_values"]["description"]: e["old_values"] for e in deletes}
    assert (
        float(by_desc["Lumber"]["amount"]) == 300.0
        and by_desc["Lumber"]["account_id"] == acct
    )
    assert float(by_desc["Nails"]["amount"]) == 20.0
    assert all(e["username"] for e in deletes)  # the actor, never NULL
    inserts = [
        e for e in _events(client, "bill_lines") if e["action"].upper() == "INSERT"
    ]
    assert any(
        float(e["new_values"]["amount"]) == 450.0 for e in inserts
    )  # the new line

    # the old journal's lines, with their amounts, and the new journal's
    jl_deletes = [
        e
        for e in _events(client, "transaction_lines")
        if e["action"].upper() == "DELETE"
    ]
    assert sorted(
        float(e["old_values"]["debit"]) + float(e["old_values"]["credit"])
        for e in jl_deletes
    ) == [
        20.0,
        300.0,
        320.0,
    ]
    jl_inserts = [
        e
        for e in _events(client, "transaction_lines")
        if e["action"].upper() == "INSERT"
    ]
    assert any(float(e["new_values"]["debit"]) == 450.0 for e in jl_inserts)
    # and the bill is what the edit says, with one journal behind it
    got = client.get(f"/api/bills/{bill['id']}").json()
    assert float(got["total"]) == 450.0 and len(got["lines"]) == 1


def test_an_invoice_edit_leaves_each_old_line_in_the_log(
    client, seed_accounts, seed_customer
):
    inv = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-09-10",
            "lines": [
                {"description": "Design", "quantity": 1, "rate": "500.00"},
                {"description": "Build", "quantity": 2, "rate": "100.00"},
            ],
        },
    )
    assert inv.status_code == 201, inv.text
    inv = inv.json()
    old_ids = {ln["id"] for ln in inv["lines"]}
    r = client.put(
        f"/api/invoices/{inv['id']}",
        json={"lines": [{"description": "Design", "quantity": 1, "rate": "650.00"}]},
    )
    assert r.status_code == 200, r.text
    deletes = [
        e for e in _events(client, "invoice_lines") if e["action"].upper() == "DELETE"
    ]
    assert {e["record_id"] for e in deletes} == old_ids
    assert sorted(float(e["old_values"]["amount"]) for e in deletes) == [200.0, 500.0]
    jl = [
        e
        for e in _events(client, "transaction_lines")
        if e["action"].upper() == "DELETE"
    ]
    assert sorted(
        float(e["old_values"]["debit"]) + float(e["old_values"]["credit"]) for e in jl
    ) == [
        200.0,
        500.0,
        700.0,
    ]


def test_the_helper_is_the_only_way_lines_are_replaced():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in (
        "app/routes/bills.py",
        "app/routes/invoices/crud.py",
        "app/routes/invoices/helpers.py",
    ):
        src = (root / rel).read_text(encoding="utf-8")
        assert "audited_bulk_delete(" in src, rel
    bills = (root / "app/routes/bills.py").read_text(encoding="utf-8")
    assert ".filter(BillLine.bill_id == bill.id).delete()" not in bills
    crud = (root / "app/routes/invoices/crud.py").read_text(encoding="utf-8")
    assert "InvoiceLine.invoice_id == invoice_id).delete()" not in crud
