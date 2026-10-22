"""Business acceptance cases for the portfolio extension, synthetic books only."""

import hashlib
import uuid
from datetime import date
import pytest
from sqlalchemy import text
from app.portfolio import models as m
from app.portfolio.service import execute, control, balance, Problem
from app.portfolio.reports import statements, overview
from app.portfolio.imports import parse_bank
from app.services.accounting import create_journal_entry
from app.services.safe_errors import DataProblem


def run(db, op, **data):
    return execute(db, {"operation": op, "key": uuid.uuid4().hex, "data": data})


def account(db, entity, code, kind, role, **kwargs):
    return run(
        db,
        "create_account",
        entity_id=entity,
        code=code,
        name=code,
        account_type=kind,
        role=role,
        **kwargs,
    )["account_id"]


def entity(db, code):
    return run(db, "create_entity", code=code, name=code)["entity_id"]


def ln(a, n, **dims):
    return {
        "account_id": a,
        "debit_cents": max(n, 0),
        "credit_cents": max(-n, 0),
        **dims,
    }


def journal(db, e, day, lines, kind="journal", **kwargs):
    return run(
        db,
        "post",
        date=day,
        description=kind,
        rationale="Synthetic acceptance evidence",
        kind=kind,
        journals=[{"entity_id": e, "lines": lines}],
        **kwargs,
    )


def bank_rows(db, a, rows, name="bank.csv"):
    return run(
        db,
        "import_bank",
        account_id=a,
        source_name=name,
        source_sha256=hashlib.sha256(name.encode()).hexdigest(),
        rows=rows,
    )["observation_ids"]


@pytest.fixture
def portfolio(db_session):
    db = db_session
    e = entity(db, "A")
    bank = account(db, e, "bank", "asset", "bank", coverage_start="2026-01-01")
    expense = account(db, e, "repairs", "expense", "expense")
    income = account(db, e, "rent", "income", "income")
    journal(
        db,
        e,
        "2026-01-01",
        [ln(bank, 100000), ln(control(db, e, "equity"), -100000)],
        kind="opening",
    )
    return db, e, bank, expense, income


def test_same_ledger_and_legacy_bypass_refused(portfolio):
    db, e, bank, expense, _ = portfolio
    with pytest.raises(DataProblem, match="portfolio"):
        create_journal_entry(
            db,
            date(2026, 1, 2),
            "bypass",
            [{"account_id": bank, "credit": 1}, {"account_id": expense, "debit": 1}],
        )
    with pytest.raises(Problem, match="balance"):
        journal(db, e, "2026-01-02", [ln(bank, -100), ln(expense, 90)])
    assert balance(db, bank) == 100000


def test_recognition_partial_settlement_and_both_statements(portfolio):
    db, e, bank, expense, _ = portfolio
    item = run(
        db,
        "recognize",
        code="DEC-BILL",
        entity_id=e,
        date="2026-12-20",
        kind="expense",
        account_id=expense,
        control_id=control(db, e, "payable"),
        amount_cents=10000,
        description="December bill",
        evidence="invoice details supplied",
    )["item_id"]

    def report(basis, start="2026-12-01", end="2026-12-31"):
        return statements(
            db, date.fromisoformat(start), date.fromisoformat(end), [e], basis=basis
        )

    accrual = report("accrual")
    cash = report("modified_cash_bank_posting")
    assert accrual["income_statement"]["net_income_cents"] == -10000
    assert accrual["balance_sheet"]["equity_cents"] == 90000
    assert cash["income_statement"]["net_income_cents"] == 0
    assert cash["balance_sheet"]["equity_cents"] == 100000
    obs = bank_rows(
        db,
        bank,
        [{"date": "2027-01-04", "amount_cents": -4000, "description": "partial bill"}],
    )[0]
    run(db, "settle", item_id=item, observation_id=obs, evidence="invoice reference")
    jan = report("modified_cash_bank_posting", "2027-01-01", "2027-01-31")
    assert jan["income_statement"]["net_income_cents"] == -4000
    assert jan["balance_sheet"]["liabilities_cents"] == 0
    assert all(v == 0 for v in jan["checks"].values())
    assert (
        report("accrual", "2027-01-01", "2027-01-31")["income_statement"][
            "net_income_cents"
        ]
        == 0
    )


def test_import_identical_rows_and_overlapping_files(portfolio):
    db, _, bank, _, _ = portfolio
    rows = [
        {
            "date": "2026-02-01",
            "amount_cents": -1000,
            "description": "Two genuine identical charges",
        }
    ] * 2
    first = bank_rows(db, bank, rows)
    assert len(first) == 2
    assert bank_rows(db, bank, rows) == []
    overlap = bank_rows(db, bank, rows, "overlap.csv")
    assert len(overlap) == 2
    assert all(
        db.get(m.BankObservation, i).status == "possible_duplicate" for i in overlap
    )
    with pytest.raises(Problem, match="distinct"):
        run(db, "post_bank", observation_id=overlap[0], rationale="unknown")


def test_suspense_correction_does_not_repeat_cash(portfolio):
    db, e, bank, expense, _ = portfolio
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -10000}])[0]
    run(db, "post_bank", observation_id=obs, rationale="Actual statement withdrawal")
    assert (
        statements(db, date(2026, 2, 1), date(2026, 2, 28), [e])["cash_flows"][
            "sections_cents"
        ]["unclassified"]
        == -10000
    )
    review = db.query(m.ReviewItem).filter_by(code="suspense").one()
    run(
        db,
        "correct_suspense",
        review_id=review.id,
        account_id=expense,
        date="2026-02-02",
        evidence="Vendor confirmed repairs",
    )
    assert balance(db, bank) == 90000
    assert balance(db, expense) == 10000
    assert balance(db, control(db, e, "suspense_debit")) == 0
    assert overview(db, date(2026, 2, 1), date(2026, 2, 28))["expense_cents"] == 10000


def test_transfer_different_dates_and_reciprocal_advance(portfolio):
    db, a, bank, _, _ = portfolio
    b = entity(db, "B")
    dest = account(db, b, "bank", "asset", "bank", coverage_start="2026-01-01")
    out = bank_rows(
        db, bank, [{"date": "2026-01-31", "amount_cents": -10000}], "out.csv"
    )[0]
    inc = bank_rows(
        db, dest, [{"date": "2026-02-02", "amount_cents": 10000}], "in.csv"
    )[0]
    run(
        db,
        "match_transfer",
        withdrawal_id=out,
        deposit_id=inc,
        purpose="advance",
        item="advance-1",
        evidence="User identified advance A to B",
    )
    assert balance(db, control(db, a, "bridge"), date(2026, 1, 31)) == 10000
    assert balance(db, control(db, a, "bridge"), date(2026, 2, 2)) == 0
    assert balance(db, control(db, a, "due_from")) == 10000
    assert balance(db, control(db, b, "due_to")) == -10000
    assert balance(db, bank) == 90000 and balance(db, dest) == 10000


def test_retry_conflict_and_atomic_rejection(portfolio):
    db, e, bank, expense, _ = portfolio
    cmd = {
        "operation": "post",
        "key": "stable",
        "data": {
            "date": "2026-02-01",
            "description": "repairs",
            "rationale": "invoice",
            "journals": [
                {
                    "entity_id": e,
                    "lines": [
                        ln(bank, -1000, cash_flow="operating"),
                        ln(expense, 1000),
                    ],
                }
            ],
        },
    }
    first = execute(db, cmd)
    assert execute(db, cmd)["event_id"] == first["event_id"]
    assert balance(db, bank) == 99000
    cmd["data"]["description"] = "different"
    with pytest.raises(Problem, match="retry key"):
        execute(db, cmd)
    b = entity(db, "B")
    bexpense = account(db, b, "expense", "expense", "expense")
    with pytest.raises(Problem, match="does not belong"):
        journal(db, e, "2026-02-02", [ln(bank, -1000), ln(bexpense, 1000)])
    assert balance(db, bank) == 99000


def test_security_and_last_month_are_liabilities(portfolio):
    db, e, bank, _, _ = portfolio
    obs = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": 120000},
            {"date": "2026-02-02", "amount_cents": 120000},
        ],
    )
    for ident, role in zip(obs, ["deposit", "last_month_rent"]):
        run(
            db,
            "post_bank",
            observation_id=ident,
            offset_account_id=control(db, e, role),
            rationale="Deposit identified by user",
        )
    report = statements(db, date(2026, 2, 1), date(2026, 2, 28), [e])
    assert report["income_statement"]["net_income_cents"] == 0
    assert report["balance_sheet"]["liabilities_cents"] == 240000


def test_noncash_acquisition_does_not_invent_cash(portfolio):
    db, e, _, _, _ = portfolio
    building = account(db, e, "building", "asset", "fixed_asset", cash_flow="investing")
    debt = account(db, e, "mortgage", "liability", "debt", cash_flow="financing")
    journal(db, e, "2026-02-01", [ln(building, 5000000), ln(debt, -5000000)])
    report = statements(db, date(2026, 2, 1), date(2026, 2, 28), [e])
    assert report["cash_flows"]["net_change_cents"] == 0
    assert report["cash_flows"]["sections_cents"] == {}
    assert len(report["cash_flows"]["noncash_events"]) == 1
    assert all(v == 0 for v in report["checks"].values())


def test_period_lock_applies_to_all_posting(portfolio):
    db, e, bank, expense, _ = portfolio
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end="2026-01-31",
        opening_cents=100000,
        ending_cents=100000,
    )
    run(
        db,
        "close_period",
        entity_id=e,
        through="2026-01-31",
        reason="Synthetic period reviewed",
    )
    with pytest.raises(Problem, match="closed through"):
        journal(db, e, "2026-01-20", [ln(bank, -100), ln(expense, 100)])


def test_csv_exact_cents_and_explicit_date_format():
    parsed = parse_bank(
        b'Date,Amount,Description\n01/02/2026,"(1,200.00)",Repairs\n',
        "bank.csv",
        {"date": "Date", "amount": "Amount", "description": "Description"},
        "%m/%d/%Y",
    )
    assert parsed["rows"][0]["date"] == "2026-01-02"
    assert parsed["rows"][0]["amount_cents"] == -120000
    with pytest.raises(Problem, match="exact to cents"):
        parse_bank(b"d,a\n2026-01-02,1.001\n", "bank.csv", {"date": "d", "amount": "a"})


def test_database_seals_posted_journal(portfolio):
    db, _, _, _, _ = portfolio
    from app.portfolio.guards import install

    install(db.connection())
    ident = db.query(m.JournalScope).first().transaction_id
    with pytest.raises(Exception, match="immutable"):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE transaction_lines SET debit=debit+1 WHERE transaction_id=:id"
                ),
                {"id": ident},
            )


def test_pooled_payment_with_explicit_holder_advance(db_session):
    db = db_session
    manager = entity(db, "MANAGER")
    a = entity(db, "OWNER")
    pool = account(
        db, manager, "pool", "asset", "bank", pooled=True, coverage_start="2026-01-01"
    )
    expense = account(db, a, "repairs", "expense", "expense")
    parties = {p.entity_id: p.id for p in db.query(m.Party)}
    own = run(
        db,
        "create_fund",
        code="MANAGER-OWN",
        holder_entity_id=manager,
        beneficiary_id=parties[manager],
        purpose="general",
    )["fund_id"]
    client = run(
        db,
        "create_fund",
        code="OWNER-FUNDS",
        holder_entity_id=manager,
        beneficiary_id=parties[a],
        purpose="general",
    )["fund_id"]
    run(
        db,
        "post",
        date="2026-01-01",
        kind="opening",
        description="Supported opening pool",
        rationale="opening statement",
        journals=[
            {
                "entity_id": manager,
                "lines": [
                    ln(pool, 20000, fund_id=own),
                    ln(pool, 10000, fund_id=client),
                    ln(
                        control(db, manager, "custody_liability"),
                        -10000,
                        fund_id=client,
                    ),
                    ln(control(db, manager, "equity"), -20000),
                ],
            },
            {
                "entity_id": a,
                "lines": [
                    ln(control(db, a, "custody_asset"), 10000, fund_id=client),
                    ln(control(db, a, "equity"), -10000),
                ],
            },
        ],
    )
    obs = bank_rows(
        db,
        pool,
        [
            {
                "date": "2026-02-01",
                "amount_cents": -15000,
                "description": "Owner repairs",
            }
        ],
    )[0]
    with pytest.raises(Problem, match="exceeds beneficiary"):
        run(
            db,
            "pool_activity",
            observation_id=obs,
            fund_id=client,
            offset_account_id=expense,
            evidence="invoice",
        )
    result = run(
        db,
        "pool_activity",
        observation_id=obs,
        fund_id=client,
        offset_account_id=expense,
        advance_from_fund_id=own,
        advance_item="ADV-1",
        evidence="Owner authorized manager advance from own funds",
    )
    assert result["advance_cents"] == 5000
    assert balance(db, pool) == 15000
    assert balance(db, pool, fund_id=client) == 0
    assert balance(db, pool, fund_id=own) == 15000
    assert balance(db, control(db, manager, "due_from")) == 5000
    assert balance(db, control(db, a, "due_to")) == -5000
    assert balance(db, expense) == 15000


def test_api_preview_commit_retry_and_stale_conflict(client):
    cmd = {
        "operation": "create_entity",
        "key": "api-A",
        "data": {"code": "API-A", "name": "API company"},
    }
    preview = client.post("/api/portfolio/proposals", json=cmd)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["preview"]["result"]["committed"] is False
    assert client.get("/api/portfolio/context").json()["entities"] == []
    url = f"/api/portfolio/proposals/{body['proposal_id']}/commit"
    result = client.post(url, json={"digest": body["digest"]})
    assert result.status_code == 200, result.text
    replay = client.post(url, json={"digest": body["digest"]})
    assert replay.json()["replayed"] is True
    cmd = {
        "operation": "create_property",
        "key": "property-1",
        "data": {"code": "P1", "name": "Property"},
    }
    proposed = client.post("/api/portfolio/proposals", json=cmd).json()
    other = client.post(
        "/api/portfolio/commands",
        json={
            "operation": "create_property",
            "key": "property-2",
            "data": {"code": "P2", "name": "Other"},
        },
    )
    assert other.status_code == 200, other.text
    stale = client.post(
        f"/api/portfolio/proposals/{proposed['proposal_id']}/commit",
        json={"digest": proposed["digest"]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "stale_proposal"


def test_api_unknown_fields_fail_and_readonly_cannot_write(client, db_session):
    response = client.post(
        "/api/portfolio/commands",
        json={
            "operation": "create_entity",
            "key": "invalid",
            "data": {"name": "A", "code": "A", "ignore_accounting_controls": True},
        },
    )
    assert response.status_code == 422, response.text
    assert db_session.query(m.Entity).count() == 0


@pytest.mark.parametrize(
    "role,kind,amount,result_sign",
    [("prepaid", "asset", -120000, -1), ("deferred", "liability", 120000, 1)],
)
def test_service_timing_profile_preserves_one_cash_movement(
    portfolio, role, kind, amount, result_sign
):
    db, e, bank, expense, income = portfolio
    target = expense if role == "prepaid" else income
    asset = account(db, e, role, kind, role)
    # Supported additional opening capital funds the annual payment.
    journal(
        db,
        e,
        "2026-01-01",
        [ln(bank, 100000), ln(control(db, e, "equity"), -100000)],
        kind="opening",
    )
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": amount}])[0]
    run(
        db,
        "service_payment",
        observation_id=obs,
        balance_account_id=asset,
        result_account_id=target,
        item="annual-service",
        evidence="annual service contract",
    )
    run(
        db,
        "service_recognition",
        entity_id=e,
        date="2026-02-28",
        balance_account_id=asset,
        result_account_id=target,
        item="annual-service",
        amount_cents=10000,
        evidence="one month delivered",
    )
    for basis, expected in [("accrual", 10000), ("modified_cash_bank_posting", 120000)]:
        r = statements(db, date(2026, 2, 1), date(2026, 2, 28), [e], basis=basis)
        assert r["income_statement"]["net_income_cents"] == expected * result_sign
        assert r["cash_flows"]["net_change_cents"] == amount
        assert all(v == 0 for v in r["checks"].values())


def test_mortgage_principal_interest_escrow(portfolio):
    db, e, bank, expense, _ = portfolio
    debt = account(db, e, "mortgage", "liability", "debt")
    escrow = account(db, e, "escrow", "asset", "escrow")
    building = account(db, e, "building", "asset", "fixed_asset")
    journal(
        db,
        e,
        "2026-01-05",
        [ln(building, 500000), ln(debt, -500000, item="Mortgage1")],
        kind="acquisition",
    )
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -20000}])[0]
    run(
        db,
        "mortgage_payment",
        observation_id=obs,
        debt_account_id=debt,
        interest_account_id=expense,
        escrow_account_id=escrow,
        principal_cents=10000,
        interest_cents=7000,
        escrow_cents=3000,
        instrument="Mortgage1",
        evidence="loan statement",
    )
    r = statements(db, date(2026, 2, 1), date(2026, 2, 28), [e])
    assert balance(db, debt) == -490000 and balance(db, escrow) == 3000
    assert r["income_statement"]["net_income_cents"] == -7000
    assert r["cash_flows"]["sections_cents"] == {
        "financing": -10000,
        "operating": -10000,
    }
    assert all(v == 0 for v in r["checks"].values())


def test_depreciation_retained_in_both_profiles_and_land_refused(portfolio):
    db, e, _, expense, _ = portfolio
    contra = account(db, e, "accumulated", "asset", "contra_asset")
    schedule = run(
        db,
        "create_schedule",
        code="building1",
        entity_id=e,
        kind="asset",
        description="Building",
        inputs={
            "cost_cents": 100000,
            "salvage_cents": 10000,
            "available_for_use": "2026-01-01",
            "asset_kind": "building",
        },
        evidence="closing and approved useful life",
    )["schedule_id"]
    run(
        db,
        "depreciate",
        schedule_id=schedule,
        date="2026-02-28",
        amount_cents=1000,
        expense_account_id=expense,
        accumulated_account_id=contra,
        evidence="Approved February schedule",
    )
    for basis in ["accrual", "modified_cash_bank_posting"]:
        r = statements(db, date(2026, 2, 1), date(2026, 2, 28), [e], basis=basis)
        assert r["income_statement"]["net_income_cents"] == -1000
        assert r["cash_flows"]["net_change_cents"] == 0
        assert all(v == 0 for v in r["checks"].values())
    with pytest.raises(Problem, match="already posted"):
        run(
            db,
            "depreciate",
            schedule_id=schedule,
            date="2026-02-28",
            amount_cents=1000,
            expense_account_id=expense,
            accumulated_account_id=contra,
            evidence="retry with wrong key",
        )
    with pytest.raises(Problem, match="remaining depreciable"):
        run(
            db,
            "depreciate",
            schedule_id=schedule,
            date="2026-03-31",
            amount_cents=90000,
            expense_account_id=expense,
            accumulated_account_id=contra,
            evidence="too much",
        )


def test_combined_eliminations_and_internal_transfer_cash(portfolio):
    db, a, bank, _, _ = portfolio
    b = entity(db, "B")
    dest = account(db, b, "bank", "asset", "bank")
    out = bank_rows(
        db, bank, [{"date": "2026-02-01", "amount_cents": -10000}], "send.csv"
    )[0]
    inc = bank_rows(
        db, dest, [{"date": "2026-02-01", "amount_cents": 10000}], "receive.csv"
    )[0]
    run(
        db,
        "match_transfer",
        withdrawal_id=out,
        deposit_id=inc,
        purpose="advance",
        item="A-B",
        evidence="documented advance",
    )
    r = statements(db, date(2026, 2, 1), date(2026, 2, 28), [a, b])
    assert r["balance_sheet"]["assets_cents"] == 100000
    assert r["balance_sheet"]["liabilities_cents"] == 0
    assert r["cash_flows"]["net_change_cents"] == 0
    assert r["cash_flows"]["sections_cents"] == {"internal_transit": 0}
    assert len(r["eliminations"]) == 2
    assert all(v == 0 for v in r["checks"].values())


@pytest.mark.parametrize("posted_sides", [(True, False), (True, True)])
def test_matching_transfer_after_suspense_preserves_bank_cash(portfolio, posted_sides):
    db, a, bank, _, _ = portfolio
    b = entity(db, "B-later")
    dest = account(db, b, "destination", "asset", "bank", coverage_start="2026-01-01")
    out = bank_rows(
        db, bank, [{"date": "2026-01-31", "amount_cents": -10000}], "late-out.csv"
    )[0]
    inc = bank_rows(
        db, dest, [{"date": "2026-02-02", "amount_cents": 10000}], "late-in.csv"
    )[0]
    for ident, posted in zip([out, inc], posted_sides):
        if posted:
            run(
                db,
                "post_bank",
                observation_id=ident,
                rationale="Known bank movement; purpose unresolved",
            )
    run(
        db,
        "match_transfer",
        withdrawal_id=out,
        deposit_id=inc,
        purpose="advance",
        item="later-match",
        evidence="Both bank references and user confirmation",
    )
    assert balance(db, bank) == 90000 and balance(db, dest) == 10000
    assert balance(db, control(db, a, "suspense_debit")) == 0
    assert balance(db, control(db, b, "suspense_credit")) == 0
    assert balance(db, control(db, a, "due_from")) == 10000
    assert db.query(m.ReviewItem).filter_by(code="suspense", status="open").count() == 0
    for basis in ["accrual", "modified_cash_bank_posting"]:
        report = statements(
            db, date(2026, 1, 1), date(2026, 2, 28), [a, b], basis=basis
        )
        assert report["cash_flows"]["net_change_cents"] == 0
        assert all(v == 0 for v in report["checks"].values())


def test_asset_revision_preserves_depreciation_history(portfolio):
    db, e, _, expense, _ = portfolio
    contra = account(db, e, "accum", "asset", "contra_asset")
    inputs = {
        "cost_cents": 10000,
        "salvage_cents": 0,
        "available_for_use": "2026-01-01",
        "asset_kind": "equipment",
    }
    first = run(
        db,
        "create_schedule",
        code="asset-original",
        entity_id=e,
        kind="asset",
        description="equipment",
        inputs=inputs,
        evidence="invoice",
    )["schedule_id"]
    run(
        db,
        "depreciate",
        schedule_id=first,
        date="2026-02-28",
        amount_cents=6000,
        expense_account_id=expense,
        accumulated_account_id=contra,
        evidence="approved schedule",
    )
    second = run(
        db,
        "create_schedule",
        code="asset-revision",
        entity_id=e,
        kind="asset",
        description="revised life",
        inputs=inputs,
        evidence="revised estimate",
        supersedes_id=first,
    )["schedule_id"]
    with pytest.raises(Problem, match="remaining depreciable"):
        run(
            db,
            "depreciate",
            schedule_id=second,
            date="2026-03-31",
            amount_cents=5000,
            expense_account_id=expense,
            accumulated_account_id=contra,
            evidence="incorrect excessive amount",
        )
    with pytest.raises(Problem, match="already posted"):
        run(
            db,
            "depreciate",
            schedule_id=second,
            date="2026-02-28",
            amount_cents=1000,
            expense_account_id=expense,
            accumulated_account_id=contra,
            evidence="duplicate period",
        )
    assert balance(db, contra) == -6000


def test_actual_bank_overdraft_remains_recorded_and_flagged(portfolio):
    db, e, bank, expense, _ = portfolio
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -150000}])[0]
    run(
        db,
        "post_bank",
        observation_id=obs,
        offset_account_id=expense,
        rationale="Statement evidence",
    )
    assert balance(db, bank) == -50000
    assert (
        db.query(m.ReviewItem)
        .filter_by(code="actual_shortage", entity_id=e, status="open")
        .count()
        == 1
    )


def test_returned_payment_reopens_item_without_duplicate_expense(portfolio):
    db, e, bank, expense, _ = portfolio
    item = run(
        db,
        "recognize",
        code="return-bill",
        entity_id=e,
        date="2026-02-01",
        kind="expense",
        account_id=expense,
        control_id=control(db, e, "payable"),
        amount_cents=10000,
        description="invoice",
        evidence="supported invoice",
    )["item_id"]
    obs = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-02", "amount_cents": -6000},
            {"date": "2026-02-03", "amount_cents": 2000},
        ],
    )
    run(db, "settle", item_id=item, observation_id=obs[0], evidence="partial payment")
    run(
        db,
        "return_settlement",
        item_id=item,
        observation_id=obs[1],
        evidence="Bank returned portion of payment",
    )
    assert balance(db, bank) == 96000
    assert balance(db, control(db, e, "payable")) == -6000
    assert (
        statements(db, date(2026, 2, 1), date(2026, 2, 28), [e])["income_statement"][
            "net_income_cents"
        ]
        == -10000
    )
    cash = statements(
        db, date(2026, 2, 1), date(2026, 2, 28), [e], basis="modified_cash_bank_posting"
    )
    assert cash["income_statement"]["net_income_cents"] == -4000
    assert all(v == 0 for v in cash["checks"].values())


def test_consolidation_adjustments_stay_out_of_entity_books(portfolio):
    db, a, bank, _, _ = portfolio
    b = entity(db, "SUB")
    investment = account(db, a, "investment", "asset", "asset")
    subbank = account(db, b, "subbank", "asset", "bank", coverage_start="2026-01-01")
    subequity = control(db, b, "equity")
    acquisition = journal(
        db,
        a,
        "2026-01-02",
        [ln(investment, 20000), ln(bank, -20000, cash_flow="investing")],
    )
    capitalization = journal(
        db,
        b,
        "2026-01-02",
        [ln(subbank, 20000, cash_flow="financing"), ln(subequity, -20000)],
    )
    group = run(
        db,
        "create_reporting_group",
        code="group-1",
        name="Approved parent and subsidiary",
        entity_ids=[a, b],
        start="2026-01-01",
        control_assessment="Supported control assessment: wholly owned subsidiary",
        reviewed_by="Synthetic reviewer",
        evidence="Synthetic ownership and control workpaper",
    )["group_id"]
    run(
        db,
        "group_adjustment",
        group_id=group,
        date="2026-01-02",
        lines=[ln(subequity, 20000), ln(investment, -20000)],
        cash_flow_event_ids=[acquisition["event_id"], capitalization["event_id"]],
        reason="Eliminate investment against subsidiary capital",
        evidence="Approved workpaper",
        reviewed_by="Synthetic reviewer",
    )
    report = statements(db, date(2026, 1, 1), date(2026, 1, 31), group_id=group)
    assert report["balance_sheet"]["assets_cents"] == 100000
    assert report["balance_sheet"]["equity_cents"] == 100000
    assert all(v == 0 for v in report["checks"].values())
    assert balance(db, investment) == 20000 and balance(db, subequity) == -20000
    assert len(report["group_adjustments"]) == 1
    assert report["cash_flows"]["sections_cents"] == {"internal_transit": 0}
    with pytest.raises(Problem, match="Cash and reciprocal"):
        run(
            db,
            "group_adjustment",
            group_id=group,
            date="2026-01-02",
            lines=[ln(bank, 100), ln(subequity, -100)],
            reason="invalid invented cash",
            evidence="test",
            reviewed_by="Synthetic reviewer",
        )


def test_shortage_requires_supported_recovery_and_reconciliation(portfolio):
    db, e, bank, expense, _ = portfolio
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -150000}])[0]
    run(
        db,
        "post_bank",
        observation_id=obs,
        offset_account_id=expense,
        rationale="Actual bank withdrawal",
    )
    review = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    with pytest.raises(Problem, match="supported entry"):
        run(db, "resolve_review", review_id=review.id, evidence="not sufficient")
    capital = account(
        db, e, "owner-funding", "equity", "contribution", cash_flow="financing"
    )
    incoming = bank_rows(
        db, bank, [{"date": "2026-02-01", "amount_cents": 50000}], "recovery.csv"
    )[0]
    recovery = run(
        db,
        "post_bank",
        observation_id=incoming,
        offset_account_id=capital,
        rationale="Supported owner contribution",
    )
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-02-01",
        end="2026-02-28",
        opening_cents=100000,
        ending_cents=0,
    )
    run(
        db,
        "resolve_review",
        review_id=review.id,
        corrective_event_id=recovery["event_id"],
        evidence="Supported funding and bank reconciliation",
    )
    assert (
        review.status == "resolved" and review.resolved_event_id == recovery["event_id"]
    )


def test_loan_and_owner_cash_classification_defaults(portfolio):
    db, e, bank, _, _ = portfolio
    debt = account(db, e, "loan-default", "liability", "debt")
    capital = account(db, e, "capital-default", "equity", "contribution")
    obs = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": 20000},
            {"date": "2026-02-02", "amount_cents": 30000},
        ],
    )
    for ident, offset in zip(obs, [debt, capital]):
        run(
            db,
            "post_bank",
            observation_id=ident,
            offset_account_id=offset,
            rationale="Supported loan or owner funding",
        )
    report = statements(db, date(2026, 2, 1), date(2026, 2, 28), [e])
    assert report["cash_flows"]["sections_cents"] == {"financing": 50000}
    assert report["income_statement"]["net_income_cents"] == 0
    unpaid = bank_rows(
        db, bank, [{"date": "2026-02-03", "amount_cents": -1000}], "ap.csv"
    )[0]
    with pytest.raises(Problem, match="linked settlement"):
        run(
            db,
            "post_bank",
            observation_id=unpaid,
            offset_account_id=control(db, e, "payable"),
            rationale="must link to recognized item",
        )


def test_zero_net_bank_correction_does_not_repeat_shortage(portfolio):
    db, e, bank, expense, _ = portfolio
    journal(
        db, e, "2026-01-02", [ln(bank, -110000), ln(expense, 110000)], observed=True
    )
    before = db.query(m.ReviewItem).filter_by(code="actual_shortage").count()
    assert before == 1
    journal(db, e, "2026-01-02", [ln(bank, 100), ln(bank, -100)])
    assert db.query(m.ReviewItem).filter_by(code="actual_shortage").count() == before
    journal(db, e, "2026-01-03", [ln(bank, -100), ln(expense, 100)], observed=True)
    assert (
        db.query(m.ReviewItem).filter_by(code="actual_shortage").count() == before + 1
    )
    assert balance(db, bank) == -10100


def test_effective_allocations_agree_between_reports_and_controls(portfolio):
    from app.portfolio.reports import ledger
    from app.models.transactions import TransactionLine
    from sqlalchemy.exc import IntegrityError

    db, e, bank, _, _ = portfolio
    prop = run(db, "create_property", code="ALLOCATION", name="Allocation property")[
        "property_id"
    ]
    bank_line = db.query(TransactionLine).filter_by(account_id=bank).one()
    original = db.get(m.LineScope, bank_line.id)
    event_id = (
        db.query(m.JournalScope)
        .filter_by(transaction_id=bank_line.transaction_id)
        .one()
        .event_id
    )
    allocation = m.LineAllocation(
        line_id=bank_line.id,
        event_id=event_id,
        date=date(2026, 1, 3),
        evidence="Synthetic allocation proof",
        components=[
            {
                "amount_cents": 60000,
                "property_id": prop,
                "unit_id": None,
                "fund_id": None,
                "counterparty_id": None,
                "item": None,
                "cash_flow": None,
            },
            {
                "amount_cents": 40000,
                "property_id": None,
                "unit_id": None,
                "fund_id": None,
                "counterparty_id": None,
                "item": None,
                "cash_flow": None,
            },
        ],
    )
    db.add(allocation)
    db.flush()
    assert balance(db, bank, date(2026, 1, 2), property_id=prop) == 0
    assert balance(db, bank, date(2026, 1, 3), property_id=prop) == 60000
    assert balance(db, bank) == 100000
    rows = ledger(db, date(2026, 1, 3), property_id=prop, account_id=bank)
    assert sum(r["debit_cents"] - r["credit_cents"] for r in rows) == 60000
    assert ledger(db, date(2026, 1, 2), property_id=prop, account_id=bank) == []
    assert original.property_id is None
    db.add(
        m.CashClassification(
            line_id=bank_line.id,
            event_id=event_id,
            date=date(2026, 1, 4),
            cash_flow="financing",
            reason="Later evidence",
        )
    )
    db.flush()
    assert all(
        r["cash_flow"] is None for r in ledger(db, date(2026, 1, 3), account_id=bank)
    )
    assert all(
        r["cash_flow"] == "financing"
        for r in ledger(db, date(2026, 1, 4), account_id=bank)
    )
    from app.portfolio.guards import install

    install(db.connection())
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text("UPDATE re_line_allocations SET evidence='rewrite' WHERE id=:id"),
            {"id": allocation.id},
        )
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "INSERT OR REPLACE INTO re_line_allocations SELECT * FROM re_line_allocations WHERE id=:id"
            ),
            {"id": allocation.id},
        )
    assert (
        db.get(m.LineAllocation, allocation.id).evidence == "Synthetic allocation proof"
    )


def test_observation_state_preserves_dimensional_residuals(portfolio):
    from app.portfolio.observation_state import observation_state

    db, e, bank, expense, _ = portfolio
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -10000}])[0]
    original = run(db, "post_bank", observation_id=obs, rationale="Statement source")
    prop = run(db, "create_property", code="SCOPE", name="Scope property")[
        "property_id"
    ]
    suspense = control(db, e, "suspense_debit")
    overlay = journal(
        db,
        e,
        "2026-02-02",
        [ln(suspense, -10000), ln(suspense, 10000, property_id=prop)],
        corrects_id=original["event_id"],
    )
    # A correction that only clears the original company scope must not be
    # reported as fully resolved merely because the account total is zero.
    journal(
        db,
        e,
        "2026-02-03",
        [ln(suspense, -10000), ln(expense, 10000)],
        corrects_id=overlay["event_id"],
    )
    before = db.query(m.Event).count()
    state = observation_state(db, obs, date(2026, 2, 3))
    residuals = state["suspense_residuals"]["accrual"]
    assert len(residuals) == 2
    assert sum(r["amount_cents"] for r in residuals) == 0
    assert {r["property_id"] for r in residuals} == {None, prop}
    assert state["bank_movements"] == [
        {"account_id": bank, "date": "2026-02-01", "amount_cents": -10000}
    ]
    assert len(observation_state(db, obs, date(2026, 2, 1))["event_ids"]) == 1
    assert (
        observation_state(db, obs, date(2026, 2, 3))["state_digest"]
        == state["state_digest"]
    )
    assert db.query(m.Event).count() == before


def test_purpose_correction_clears_effective_property_scope(portfolio):
    from app.portfolio.observation_state import observation_state

    db, e, bank, expense, _ = portfolio
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -10000}])[0]
    original = run(db, "post_bank", observation_id=obs, rationale="Statement source")
    prop = run(db, "create_property", code="CORRECT", name="Correct property")[
        "property_id"
    ]
    suspense = control(db, e, "suspense_debit")
    journal(
        db,
        e,
        "2026-02-01",
        [ln(suspense, -10000), ln(suspense, 10000, property_id=prop)],
        corrects_id=original["event_id"],
    )
    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    run(
        db,
        "correct_suspense",
        review_id=review.id,
        account_id=expense,
        property_id=prop,
        date="2026-02-01",
        evidence="Verified property and purpose",
    )
    state = observation_state(db, obs)
    assert state["suspense_residuals"] == {
        "accrual": [],
        "modified_cash_bank_posting": [],
    }
    assert balance(db, bank) == 90000
    assert balance(db, expense, property_id=prop) == 10000
    assert review.status == "resolved"


def test_posted_pool_receipt_changes_custody_without_repeating_cash(db_session):
    from app.portfolio.observation_state import observation_state
    from app.models.transactions import TransactionLine

    db = db_session
    holder = entity(db, "HOLDER")
    bank = account(db, holder, "pool", "asset", "bank", pooled=True)
    unknown = run(db, "create_party", code="UNKNOWN", name="Unknown beneficiary")[
        "party_id"
    ]
    client = run(db, "create_party", code="CLIENT", name="External client")["party_id"]
    unresolved = run(
        db,
        "create_fund",
        code="UNRESOLVED",
        holder_entity_id=holder,
        beneficiary_id=unknown,
        purpose="unresolved",
    )["fund_id"]
    fund = run(
        db,
        "create_fund",
        code="CLIENT",
        holder_entity_id=holder,
        beneficiary_id=client,
        purpose="general",
    )["fund_id"]
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": 33000}])[0]
    original = run(
        db,
        "post_bank",
        observation_id=obs,
        fund_id=unresolved,
        rationale="Unknown receipt",
    )
    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    physical = [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ]
    data = dict(
        observation_id=obs,
        review_id=review.id,
        fund_id=fund,
        expected_state_digest=observation_state(db, obs)["state_digest"],
        evidence="Owner funding verified",
    )
    key = uuid.uuid4().hex
    result = execute(db, {"operation": "correct_posted_pool", "key": key, "data": data})
    assert result["bank_cash_delta_cents"] == 0
    assert execute(db, {"operation": "correct_posted_pool", "key": key, "data": data})[
        "replayed"
    ]
    assert [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ] == physical
    assert db.get(m.BankObservation, obs).event_id == original["event_id"]
    assert balance(db, bank, fund_id=unresolved) == 0
    assert balance(db, bank, fund_id=fund) == 33000
    assert balance(db, control(db, holder, "custody_liability"), fund_id=fund) == -33000
    assert review.status == "resolved"
    assert observation_state(db, obs)["suspense_residuals"]["accrual"] == []
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert all(
            v == 0
            for v in statements(
                db, date(2026, 1, 1), date(2026, 12, 31), [holder], basis=basis
            )["checks"].values()
        )


def test_atomic_batch_rolls_back_and_replays(portfolio):
    db, e, bank, expense, _ = portfolio
    original_events = db.query(m.Event).count()
    operation = {
        "operation": "post",
        "data": {
            "date": "2026-02-01",
            "description": "Batch test",
            "rationale": "Synthetic evidence",
            "journals": [{"entity_id": e, "lines": [ln(bank, -100), ln(expense, 100)]}],
        },
    }
    key = uuid.uuid4().hex
    with pytest.raises(Problem):
        execute(
            db,
            {
                "key": key,
                "operation": "batch",
                "data": {
                    "operations": [operation, {"operation": "not_real", "data": {}}]
                },
            },
        )
    assert balance(db, bank) == 100000
    assert db.query(m.Event).count() == original_events
    assert db.get(m.RequestRecord, key) is None
    command = {
        "key": key,
        "operation": "batch",
        "data": {"operations": [operation, operation]},
    }
    result = execute(db, command)
    assert result["atomic"] and result["operation_count"] == 2
    assert len(set(result["event_ids"])) == 2
    assert balance(db, bank) == 99800
    assert execute(db, command)["replayed"]
    assert balance(db, bank) == 99800
    with pytest.raises(Problem, match="different contents"):
        execute(db, {**command, "data": {"operations": [operation]}})
    for forbidden in ("batch", "close_period", "reopen_period"):
        with pytest.raises(Problem, match="separately"):
            run(db, "batch", operations=[{"operation": forbidden, "data": {}}])


def test_posted_pool_split_keeps_client_and_security_funds_distinct(db_session):
    from app.portfolio.observation_state import observation_state
    from app.models.transactions import TransactionLine

    db = db_session
    holder = entity(db, "MIXED")
    bank = account(db, holder, "pool", "asset", "bank", pooled=True)
    party = run(db, "create_party", code="CLIENT", name="Client")["party_id"]
    funds = [
        run(
            db,
            "create_fund",
            code=purpose,
            holder_entity_id=holder,
            beneficiary_id=party,
            purpose=purpose,
            restriction=restriction,
        )["fund_id"]
        for purpose, restriction in [
            ("unresolved", "unrestricted"),
            ("general", "unrestricted"),
            ("security_deposit", "tenant"),
        ]
    ]
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": 100000}])[0]
    run(db, "post_bank", observation_id=obs, fund_id=funds[0], rationale="Statement")
    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    data = dict(
        observation_id=obs,
        review_id=review.id,
        expected_state_digest=observation_state(db, obs)["state_digest"],
        evidence="Issued owner statement establishes key fee and tenant deposit",
        components=[
            dict(
                observation_id=obs,
                amount_cents=amount,
                fund_id=fund,
                evidence="Component proof",
            )
            for amount, fund in [(15000, funds[1]), (85000, funds[2])]
        ],
    )
    before = db.query(TransactionLine).filter_by(account_id=bank).count()
    bad = {**data, "components": data["components"] + [data["components"][0]]}
    with pytest.raises(Problem, match="remaining bank suspense"):
        run(db, "correct_posted_pool_split", **bad)
    assert review.status == "open"
    run(db, "correct_posted_pool_split", **data)
    assert db.query(TransactionLine).filter_by(account_id=bank).count() == before
    assert balance(db, bank, fund_id=funds[0]) == 0
    assert balance(db, bank, fund_id=funds[1]) == 15000
    assert balance(db, bank, fund_id=funds[2]) == 85000
    assert review.status == "resolved"
    custody = control(db, holder, "custody_liability")
    assert balance(db, custody, fund_id=funds[1]) == -15000
    assert balance(db, custody, fund_id=funds[2]) == -85000


def test_posted_pool_partial_resumes_without_repeating_cash(db_session):
    from app.portfolio.observation_state import observation_state
    from app.models.transactions import TransactionLine

    db = db_session
    holder = entity(db, "PARTIAL")
    bank = account(db, holder, "pool", "asset", "bank", pooled=True)
    party = run(db, "create_party", code="CLIENT", name="Client")["party_id"]
    funds = [
        run(
            db,
            "create_fund",
            code=code,
            holder_entity_id=holder,
            beneficiary_id=party,
            purpose=purpose,
        )["fund_id"]
        for code, purpose in [
            ("UNKNOWN", "unresolved"),
            ("A", "general"),
            ("B", "general"),
        ]
    ]
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": 100000}])[0]
    run(db, "post_bank", observation_id=obs, fund_id=funds[0], rationale="Statement")
    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    original = [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ]
    first_digest = observation_state(db, obs)["state_digest"]
    for amount, fund, remaining in [(15000, funds[1], 85000), (85000, funds[2], 0)]:
        data = dict(
            observation_id=obs,
            review_id=review.id,
            expected_state_digest=observation_state(db, obs)["state_digest"],
            evidence="Documented component",
            components=[
                dict(
                    observation_id=obs,
                    amount_cents=amount,
                    fund_id=fund,
                    evidence="Component proof",
                )
            ],
        )
        if remaining == 0:
            with pytest.raises(Problem, match="changed"):
                run(
                    db,
                    "correct_posted_pool_split",
                    **{**data, "expected_state_digest": first_digest},
                )
        result = run(db, "correct_posted_pool_split", **data)
        assert result["remaining_cents"] == remaining
        assert review.status == ("open" if remaining else "resolved")
        assert balance(db, bank, fund_id=funds[0]) == remaining
    assert balance(db, bank, fund_id=funds[1]) == 15000
    assert balance(db, bank, fund_id=funds[2]) == 85000
    assert [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ] == original
    assert observation_state(db, obs)["suspense_residuals"]["accrual"] == []


def test_review_filters_are_explicit_and_count_before_pagination(portfolio):
    from app.portfolio.routes import records
    from fastapi import HTTPException

    db, e, bank, _, _ = portfolio
    ids = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": 100},
            {"date": "2026-03-01", "amount_cents": 200},
        ],
    )
    for obs in ids:
        run(db, "post_bank", observation_id=obs, rationale="Statement")
    result = records(
        "reviews",
        account_id=bank,
        code="suspense",
        status="open",
        start=date(2026, 2, 1),
        end=date(2026, 2, 28),
        offset=0,
        limit=1,
        db=db,
    )
    assert result["total"] == 1
    assert result["rows"][0]["observation_id"] == ids[0]
    assert records("reviews", code="suspense", offset=1, limit=1, db=db)["total"] == 2
    assert (
        records(
            "reviews", observation_id=ids[1], code="suspense", offset=0, limit=1, db=db
        )["total"]
        == 1
    )
    with pytest.raises(HTTPException, match="date filtering"):
        records("entities", start=date(2026, 1, 1), offset=0, limit=1, db=db)
    with pytest.raises(HTTPException, match="Unsupported"):
        records("accounts", status="open", offset=0, limit=1, db=db)


def test_posted_settlement_clears_suspense_without_repeating_bank_or_item(portfolio):
    from app.portfolio.observation_state import observation_state
    from app.models.transactions import TransactionLine

    db, e, bank, expense, _ = portfolio
    item = run(
        db,
        "recognize",
        code="POSTED-BILL",
        entity_id=e,
        date="2026-01-01",
        kind="expense",
        account_id=expense,
        control_id=control(db, e, "payable"),
        amount_cents=10000,
        description="Recognized bill",
        evidence="Invoice",
    )["item_id"]
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -4000}])[0]
    original = run(db, "post_bank", observation_id=obs, rationale="Statement")
    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    physical = [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ]
    command = dict(
        operation="correct_posted_settlement",
        key=uuid.uuid4().hex,
        data=dict(
            item_id=item,
            observation_id=obs,
            review_id=review.id,
            expected_state_digest=observation_state(db, obs)["state_digest"],
            evidence="Invoice matched to payment",
        ),
    )
    execute(db, command)
    assert execute(db, command)["replayed"]
    assert db.get(m.BankObservation, obs).event_id == original["event_id"]
    assert [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ] == physical
    assert (
        db.query(m.ItemApplication).filter_by(item_id=item).one().amount_cents == 4000
    )
    assert review.status == "resolved"
    assert observation_state(db, obs)["suspense_residuals"]["accrual"] == []
    for basis in ("accrual", "modified_cash_bank_posting"):
        report = statements(db, date(2026, 2, 1), date(2026, 2, 28), [e], basis=basis)
        assert all(v == 0 for v in report["checks"].values())
        assert report["income_statement"]["net_income_cents"] == (
            -4000 if basis == "modified_cash_bank_posting" else 0
        )


def test_context_identifies_company_and_command_contract(db_session):
    from app.models.settings import Settings
    from app.portfolio.routes import context, commands
    from app.portfolio.service import digest

    db_session.add(Settings(key="company_name", value="Synthetic test company"))
    db_session.flush()
    identity = context(db_session)["connection"]
    assert identity["company_name"] == "Synthetic test company"
    assert identity["command_schema_digest"] == digest(commands())
    assert identity["portfolio_release"] == "2026.10-pooled-corrections.2"
    assert identity["database_engine"] == "sqlite"


@pytest.mark.parametrize("funding", [100000, 20000])
def test_posted_internal_pool_transfer_preserves_both_source_legs(db_session, funding):
    from app.portfolio.observation_state import observation_state
    from app.models.transactions import TransactionLine

    db = db_session
    holder = entity(db, "TRANSFER")
    banks = [
        account(
            db, holder, code, "asset", "bank", pooled=True, coverage_start="2026-01-01"
        )
        for code in ("A", "B")
    ]
    party = run(db, "create_party", code="CLIENT", name="Client")["party_id"]
    funds = [
        run(
            db,
            "create_fund",
            code=purpose,
            holder_entity_id=holder,
            beneficiary_id=party,
            purpose=purpose,
        )["fund_id"]
        for purpose in ("unresolved", "general")
    ]
    seed = bank_rows(
        db, banks[0], [{"date": "2026-01-01", "amount_cents": funding}], "seed.csv"
    )[0]
    run(
        db,
        "pool_activity",
        observation_id=seed,
        fund_id=funds[1],
        evidence="Client funds",
    )
    specs = []
    original_events = []
    for bank, amount in zip(banks, (-30000, 30000)):
        obs = bank_rows(
            db, bank, [{"date": "2026-02-01", "amount_cents": amount}], f"{bank}.csv"
        )[0]
        result = run(
            db, "post_bank", observation_id=obs, fund_id=funds[0], rationale="Statement"
        )
        original_events.append(result["event_id"])
        review = (
            db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
        )
        specs.append(
            dict(
                observation_id=obs,
                review_id=review.id,
                expected_state_digest=observation_state(db, obs)["state_digest"],
            )
        )
    before = [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter(TransactionLine.account_id.in_(banks))
    ]
    command = dict(
        operation="correct_posted_internal_transfer",
        key=uuid.uuid4().hex,
        data=dict(
            withdrawal=specs[0],
            deposit=specs[1],
            fund_id=funds[1],
            evidence="Both statements and transfer confirmation",
        ),
    )
    if funding < 30000:
        event_count = db.query(m.Event).count()
        allocation_count = db.query(m.LineAllocation).count()
        with pytest.raises(Problem, match="insufficient supported holdings"):
            execute(db, command)
        assert db.query(m.Event).count() == event_count
        assert db.query(m.LineAllocation).count() == allocation_count
        assert all(
            db.get(m.ReviewItem, spec["review_id"]).status == "open" for spec in specs
        )
        assert db.query(m.RequestRecord).filter_by(key=command["key"]).count() == 0
        return
    execute(db, command)
    assert execute(db, command)["replayed"]
    assert [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter(TransactionLine.account_id.in_(banks))
    ] == before
    assert [
        db.get(m.BankObservation, spec["observation_id"]).event_id for spec in specs
    ] == original_events
    assert [balance(db, bank, fund_id=funds[1]) for bank in banks] == [70000, 30000]
    for spec in specs:
        assert db.get(m.ReviewItem, spec["review_id"]).status == "resolved"
        assert (
            observation_state(db, spec["observation_id"])["suspense_residuals"][
                "accrual"
            ]
            == []
        )
    for basis in ("accrual", "modified_cash_bank_posting"):
        report = statements(
            db, date(2026, 2, 1), date(2026, 2, 28), [holder], basis=basis
        )
        assert all(v == 0 for v in report["checks"].values())

    from app.portfolio.assessments import shortage_assessment

    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").first()
    assessment = shortage_assessment(db, alert.id, date(2026, 2, 1))
    assert assessment["classification"] == "allocation_artifact_candidate"
    assert assessment["effective_historical_balance_cents"] == 0
    assert alert.status == "open"


def test_shortage_assessment_retains_historical_shortfall_and_is_read_only(portfolio):
    from app.portfolio.assessments import shortage_assessment

    db, e, bank, expense, income = portfolio
    journal(
        db, e, "2026-02-01", [ln(bank, -110000), ln(expense, 110000)], observed=True
    )
    review = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    before = shortage_assessment(db, review.id, date(2026, 2, 1))
    assert before["classification"] == "unresolved_shortfall"
    assert before["effective_historical_balance_cents"] == -10000
    journal(db, e, "2026-03-01", [ln(bank, 20000), ln(income, -20000)], observed=True)
    counts = (db.query(m.Event).count(), db.query(m.RequestRecord).count())
    after = shortage_assessment(db, review.id, date(2026, 3, 1))
    assert after["classification"] == "historical_shortfall_later_nonnegative"
    assert after["effective_historical_balance_cents"] == -10000
    assert after["effective_current_balance_cents"] == 10000
    assert len(after["later_receipts"]) == 1
    assert shortage_assessment(db, review.id, date(2026, 3, 1)) == after
    assert review.status == "open"
    assert (db.query(m.Event).count(), db.query(m.RequestRecord).count()) == counts
    unknown = account(db, e, "UNKNOWN-COVERAGE", "asset", "bank")
    journal(db, e, "2026-02-01", [ln(unknown, -100), ln(expense, 100)], observed=True)
    other = (
        db.query(m.ReviewItem)
        .filter_by(code="actual_shortage")
        .order_by(m.ReviewItem.id.desc())
        .first()
    )
    assert (
        shortage_assessment(db, other.id, date(2026, 3, 1))["classification"]
        == "insufficient_source_coverage"
    )


def test_posted_intercompany_advance_keeps_funding_uncertainty_and_cash(db_session):
    from app.portfolio.observation_state import observation_state
    from app.models.transactions import TransactionLine

    db = db_session
    entities = [entity(db, code) for code in ("CPM", "RPC")]
    banks = [account(db, e, "bank", "asset", "bank", pooled=True) for e in entities]
    party = run(db, "create_party", code="UNKNOWN", name="Unresolved owner")["party_id"]
    funds = [
        run(
            db,
            "create_fund",
            code=f"U{e}",
            holder_entity_id=e,
            beneficiary_id=party,
            purpose="unresolved",
        )["fund_id"]
        for e in entities
    ]
    specs, original = [], []
    for bank, fund, amount in zip(banks, funds, (-700000, 700000)):
        obs = bank_rows(
            db, bank, [{"date": "2026-02-01", "amount_cents": amount}], f"{bank}.csv"
        )[0]
        original.append(
            run(
                db, "post_bank", observation_id=obs, fund_id=fund, rationale="Statement"
            )["event_id"]
        )
        review = (
            db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
        )
        specs.append(
            dict(
                observation_id=obs,
                review_id=review.id,
                expected_state_digest=observation_state(db, obs)["state_digest"],
            )
        )
    physical = [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter(TransactionLine.account_id.in_(banks))
    ]
    command = dict(
        operation="correct_posted_intercompany",
        key=uuid.uuid4().hex,
        data=dict(
            withdrawal=specs[0],
            deposit=specs[1],
            purpose="advance",
            item="RPC-7000",
            date="2026-02-01",
            evidence="User confirmed loan purpose; funding source not established",
        ),
    )
    result = execute(db, command)
    assert len(result["open_funding_review_ids"]) == 2
    assert execute(db, command)["replayed"]
    assert balance(db, control(db, entities[0], "due_from"), item="RPC-7000") == 700000
    assert balance(db, control(db, entities[1], "due_to"), item="RPC-7000") == -700000
    assert [
        (r.id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter(TransactionLine.account_id.in_(banks))
    ] == physical
    assert [
        db.get(m.BankObservation, spec["observation_id"]).event_id for spec in specs
    ] == original
    for spec in specs:
        assert (
            observation_state(db, spec["observation_id"])["suspense_residuals"][
                "accrual"
            ]
            == []
        )
    assert (
        db.query(m.ReviewItem).filter_by(code="actual_shortage", status="open").count()
        > 0
    )
    for basis in ("accrual", "modified_cash_bank_posting"):
        report = statements(
            db, date(2026, 2, 1), date(2026, 2, 28), entities, basis=basis
        )
        assert all(v == 0 for v in report["checks"].values())
    repayment_specs = []
    for bank, fund, amount in zip(reversed(banks), reversed(funds), (-200000, 200000)):
        obs = bank_rows(
            db,
            bank,
            [{"date": "2026-03-01", "amount_cents": amount}],
            f"repay-{bank}.csv",
        )[0]
        run(
            db,
            "post_bank",
            observation_id=obs,
            fund_id=fund,
            rationale="Repayment statement",
        )
        review = (
            db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
        )
        repayment_specs.append(
            dict(
                observation_id=obs,
                review_id=review.id,
                expected_state_digest=observation_state(db, obs)["state_digest"],
            )
        )
    repayment = dict(
        withdrawal=repayment_specs[0],
        deposit=repayment_specs[1],
        purpose="repayment",
        item="RPC-7000",
        date="2026-03-01",
        evidence="Confirmed partial repayment",
    )
    with pytest.raises(Problem, match="exact entity pair"):
        run(db, "correct_posted_intercompany", **{**repayment, "item": "UNRELATED"})
    assert all(
        db.get(m.ReviewItem, spec["review_id"]).status == "open"
        for spec in repayment_specs
    )
    run(db, "correct_posted_intercompany", **repayment)
    assert balance(db, control(db, entities[0], "due_from"), item="RPC-7000") == 500000
    assert balance(db, control(db, entities[1], "due_to"), item="RPC-7000") == -500000


def test_party_receivable_and_loan_remain_distinct_without_false_income(portfolio):
    from app.portfolio.observation_state import observation_state
    from app.models.transactions import TransactionLine

    db, e, bank, _, _ = portfolio
    party = run(db, "create_party", code="JOSEPH", name="Joseph")["party_id"]
    items = []
    for amount, kind, code in [
        (-7179, "party_receivable", "JOSEPH-ADVANCE"),
        (50000, "party_payable", "JOSEPH-LOAN"),
    ]:
        obs = bank_rows(
            db, bank, [{"date": "2026-02-01", "amount_cents": amount}], code + ".csv"
        )[0]
        original = run(db, "post_bank", observation_id=obs, rationale="Statement")
        review = (
            db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
        )
        before = [
            (r.id, r.debit, r.credit)
            for r in db.query(TransactionLine).filter_by(account_id=bank)
        ]
        command = dict(
            operation="correct_posted_party_balance",
            key=uuid.uuid4().hex,
            data=dict(
                observation=dict(
                    observation_id=obs,
                    review_id=review.id,
                    expected_state_digest=observation_state(db, obs)["state_digest"],
                ),
                party_id=party,
                code=code,
                kind=kind,
                date="2026-02-01",
                evidence="User confirms separate debt balances",
            ),
        )
        result = execute(db, command)
        assert execute(db, command)["replayed"]
        items.append(result["item_id"])
        assert db.get(m.Event, result["event_id"]).source["party_id"] == party
        assert db.get(m.BankObservation, obs).event_id == original["event_id"]
        assert [
            (r.id, r.debit, r.credit)
            for r in db.query(TransactionLine).filter_by(account_id=bank)
        ] == before
        for line in (
            db.query(m.LineScope)
            .join(TransactionLine, m.LineScope.line_id == TransactionLine.id)
            .join(
                m.JournalScope,
                m.JournalScope.transaction_id == TransactionLine.transaction_id,
            )
            .filter(m.JournalScope.event_id == result["event_id"])
        ):
            assert line.counterparty_id is None
    assert balance(db, control(db, e, "receivable"), item="JOSEPH-ADVANCE") == 7179
    assert balance(db, control(db, e, "payable"), item="JOSEPH-LOAN") == -50000
    receipt = bank_rows(
        db, bank, [{"date": "2026-03-01", "amount_cents": 7179}], "repayment.csv"
    )[0]
    run(
        db,
        "settle",
        observation_id=receipt,
        item_id=items[0],
        evidence="Repayment of Joseph advance",
    )
    assert balance(db, control(db, e, "receivable"), item="JOSEPH-ADVANCE") == 0
    assert balance(db, control(db, e, "payable"), item="JOSEPH-LOAN") == -50000
    for basis in ("accrual", "modified_cash_bank_posting"):
        report = statements(db, date(2026, 2, 1), date(2026, 3, 31), [e], basis=basis)
        assert report["income_statement"]["net_income_cents"] == 0
        assert all(v == 0 for v in report["checks"].values())


def test_validate_existing_correction_requires_lineage_and_complete_scopes(portfolio):
    from app.portfolio.observation_state import observation_state

    db, e, bank, expense, _ = portfolio
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": -15000}])[0]
    original = run(db, "post_bank", observation_id=obs, rationale="Statement")
    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    correction = journal(
        db,
        e,
        "2026-02-01",
        [ln(control(db, e, "suspense_debit"), -15000), ln(expense, 15000)],
        corrects_id=original["event_id"],
    )
    unrelated = journal(
        db, e, "2026-02-01", [ln(expense, 1), ln(control(db, e, "equity"), -1)]
    )
    data = dict(
        observation=dict(
            observation_id=obs,
            review_id=review.id,
            expected_state_digest=observation_state(db, obs)["state_digest"],
        ),
        corrective_event_id=correction["event_id"],
        balances=[dict(account_id=expense, amount_cents=15000)],
        evidence="Verified complete existing purpose correction",
    )
    with pytest.raises(Problem, match="explicitly linked"):
        run(
            db,
            "validate_existing_correction",
            **{**data, "corrective_event_id": unrelated["event_id"]},
        )
    with pytest.raises(Problem, match="stated scope"):
        run(
            db,
            "validate_existing_correction",
            **{**data, "balances": [dict(account_id=expense, amount_cents=14000)]},
        )
    before = db.query(m.Event).count()
    command = dict(
        operation="validate_existing_correction", key=uuid.uuid4().hex, data=data
    )
    result = execute(db, command)
    assert result["new_financial_events"] == 0
    assert execute(db, command)["replayed"]
    assert db.query(m.Event).count() == before
    assert review.status == "resolved"
    assert db.get(m.BankObservation, obs).event_id == original["event_id"]


def test_property_record_filters_use_effective_bank_allocations(portfolio):
    from app.portfolio.routes import records
    from app.models.transactions import TransactionLine

    db, e, bank, _, _ = portfolio
    prop = run(db, "create_property", code="FILTER", name="Effective property")[
        "property_id"
    ]
    line = db.query(TransactionLine).filter_by(account_id=bank).one()
    event_id = (
        db.query(m.JournalScope)
        .filter_by(transaction_id=line.transaction_id)
        .one()
        .event_id
    )
    db.add(
        m.LineAllocation(
            line_id=line.id,
            event_id=event_id,
            date=date(2026, 1, 3),
            evidence="Supported property split",
            components=[
                dict(
                    amount_cents=100000,
                    property_id=prop,
                    unit_id=None,
                    fund_id=None,
                    counterparty_id=None,
                    item=None,
                    cash_flow=None,
                )
            ],
        )
    )
    db.flush()
    assert (
        records(
            "events", property_id=prop, end=date(2026, 1, 2), offset=0, limit=20, db=db
        )["total"]
        == 0
    )
    assert (
        records(
            "events", property_id=prop, end=date(2026, 1, 3), offset=0, limit=20, db=db
        )["rows"][0]["id"]
        == event_id
    )


def test_import_preserves_physical_rows_separate_from_normalized_indexes(portfolio):
    db, _, bank, _, _ = portfolio
    content = b'd,a,m\n2026-02-01,1,"first\ncontinued"\n,,\n\n2026-02-02,2,second\n'
    parsed = parse_bank(
        content, "original.csv", {"date": "d", "amount": "a", "description": "m"}
    )
    assert [row["physical_row"] for row in parsed["rows"]] == [2, 6]
    result = run(db, "import_bank", account_id=bank, **parsed)
    sources = [
        db.get(m.BankObservation, ident).source for ident in result["observation_ids"]
    ]
    assert [s["physical_row"] for s in sources] == [2, 6]
    assert [s["normalized_record_index"] for s in sources] == [1, 2]
    assert all(s["sha256"] == hashlib.sha256(content).hexdigest() for s in sources)


def test_allocation_migration_refuses_to_discard_recorded_history(
    portfolio, monkeypatch
):
    import importlib
    from app.models.transactions import TransactionLine

    db, _, bank, _, _ = portfolio
    migration = importlib.import_module(
        "migrations.versions.bb0002_effective_allocations"
    )
    line = db.query(TransactionLine).filter_by(account_id=bank).one()
    event = db.query(m.JournalScope).filter_by(transaction_id=line.transaction_id).one()
    db.add(
        m.LineAllocation(
            line_id=line.id,
            event_id=event.event_id,
            date=date(2026, 1, 1),
            components=[dict(amount_cents=100000)],
            evidence="Preserved history",
        )
    )
    db.flush()
    monkeypatch.setattr(migration.op, "get_bind", lambda: db.connection())
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        migration.downgrade()
    assert db.query(m.LineAllocation).count() == 1


@pytest.fixture
def correction_pool(db_session):
    db = db_session
    holder = entity(db, "CP")
    bank = account(
        db, holder, "pool", "asset", "bank", pooled=True, coverage_start="2026-01-01"
    )
    unknown = run(db, "create_party", code="UNKNOWN", name="Unknown")["party_id"]
    client = run(db, "create_party", code="CLIENT", name="Client")["party_id"]
    funds = []
    for code, party, purpose in [
        ("U", unknown, "unresolved"),
        ("U2", unknown, "unresolved"),
        ("C", client, "general"),
    ]:
        funds.append(
            run(
                db,
                "create_fund",
                code=code,
                holder_entity_id=holder,
                beneficiary_id=party,
                purpose=purpose,
            )["fund_id"]
        )
    return db, holder, bank, funds


def pool_correction(db, obs, fund):
    from app.portfolio.observation_state import observation_state

    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    return dict(
        observation_id=obs,
        review_id=review.id,
        fund_id=fund,
        expected_state_digest=observation_state(db, obs)["state_digest"],
        evidence="Verified source",
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_legacy_zero_net_bank_history_preserved(correction_pool, legacy):
    from app.models.transactions import TransactionLine
    from app.portfolio.observation_state import observation_state

    db, holder, bank, (unknown, second, client) = correction_pool
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": 240000}])[0]
    original = run(
        db, "post_bank", observation_id=obs, fund_id=unknown, rationale="Unidentified"
    )
    if legacy:
        journal(
            db,
            holder,
            "2026-02-01",
            [ln(bank, -240000, fund_id=unknown), ln(bank, 240000, fund_id=second)],
            corrects_id=original["event_id"],
            source={"observation_id": obs},
        )
    physical = [
        (r.id, r.account_id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ]
    data = pool_correction(db, obs, client)
    command = {
        "operation": "correct_posted_pool",
        "key": uuid.uuid4().hex,
        "data": data,
    }
    result = execute(db, command)
    assert execute(db, command)["replayed"]
    assert result["review_lifecycle"]["gross_closures"] == 1
    assert physical == [
        (r.id, r.account_id, r.debit, r.credit)
        for r in db.query(TransactionLine).filter_by(account_id=bank)
    ]
    assert balance(db, bank, fund_id=client) == 240000
    assert balance(db, bank, fund_id=unknown) == balance(db, bank, fund_id=second) == 0
    assert not any(observation_state(db, obs)["suspense_residuals"].values())
    for basis in ("accrual", "modified_cash_bank_posting"):
        assert all(
            v == 0
            for v in statements(
                db, date(2026, 1, 1), date(2026, 12, 31), [holder], basis=basis
            )["checks"].values()
        )


def test_existing_withdrawal_does_not_require_funding_twice(correction_pool):
    db, holder, bank, (unknown, second, client) = correction_pool
    receipt, payment = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": 250000},
            {"date": "2026-02-01", "amount_cents": -250000},
        ],
    )
    run(
        db,
        "pool_activity",
        observation_id=receipt,
        fund_id=client,
        evidence="Verified receipt",
    )
    original = run(
        db, "post_bank", observation_id=payment, fund_id=unknown, rationale="Payment"
    )
    # Synthetic legacy state: bank attribution was corrected but the offset
    # remained suspense. This is not a production mutation or public bypass.
    from app.portfolio.reports import ledger

    cash = next(
        r
        for r in ledger(db, date(2026, 2, 1), event_ids=[original["event_id"]])
        if r["role"] == "bank"
    )
    db.add(
        m.LineAllocation(
            line_id=cash["line_id"],
            event_id=original["event_id"],
            date=date(2026, 2, 1),
            evidence="Legacy attribution fixture",
            components=[{"amount_cents": -250000, "fund_id": client}],
        )
    )
    db.flush()
    assert balance(db, bank, fund_id=client) == 0
    result = run(db, "correct_posted_pool", **pool_correction(db, payment, client))
    assert result["advance_cents"] == 0
    assert balance(db, control(db, holder, "custody_liability"), fund_id=client) == 0


def test_exposed_source_deficit_is_diagnosed_and_atomic(correction_pool):
    db, holder, bank, (unknown, second, client) = correction_pool
    receipt, payment = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": 33000},
            {"date": "2026-02-01", "amount_cents": -20000},
        ],
    )
    for obs in (receipt, payment):
        run(
            db,
            "post_bank",
            observation_id=obs,
            fund_id=unknown,
            rationale="Unidentified",
        )
    before = db.query(m.Event).count()
    with pytest.raises(Problem) as error:
        run(db, "correct_posted_pool", **pool_correction(db, receipt, client))
    assert error.value.code == "allocation_source_deficit"
    assert error.value.details["after_cents"] == -20000
    assert error.value.details["physical_cash_delta_cents"] == 0
    assert db.query(m.Event).count() == before
    assert db.query(m.LineAllocation).count() == 0


def test_historical_alert_resolution_does_not_use_later_deficit(portfolio):
    from app.portfolio.assessments import shortage_assessment

    db, e, bank, expense, income = portfolio
    outgoing, incoming, later = bank_rows(
        db,
        bank,
        [
            {"date": "2026-02-01", "amount_cents": -150000},
            {"date": "2026-02-01", "amount_cents": 50000},
            {"date": "2026-03-01", "amount_cents": -10000},
        ],
    )
    run(
        db,
        "post_bank",
        observation_id=outgoing,
        offset_account_id=expense,
        rationale="Actual payment",
    )
    alert = db.query(m.ReviewItem).filter_by(code="actual_shortage").one()
    run(
        db,
        "post_bank",
        observation_id=incoming,
        offset_account_id=income,
        rationale="Actual receipt",
    )
    run(
        db,
        "post_bank",
        observation_id=later,
        offset_account_id=expense,
        rationale="Later payment",
    )
    assessment = shortage_assessment(db, alert.id, date(2026, 2, 28))
    data = dict(
        review_id=alert.id,
        as_of="2026-02-28",
        expected_assessment_digest=assessment["assessment_digest"],
        corrective_event_id=alert.event_id,
        reason="import_order_artifact",
        evidence="Same-day receipt supported",
        coverage_evidence="Complete bank statements and verified opening",
    )
    with pytest.raises(Problem) as error:
        run(db, "resolve_historical_shortage", **data)
    assert error.value.code == "missing_reconciliation"
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end="2026-02-28",
        opening_cents=100000,
        ending_cents=0,
    )
    result = run(db, "resolve_historical_shortage", **data)
    assert result["financial_entries_added"] is False
    assert db.get(m.ReviewItem, alert.id).status == "resolved"
    assert (
        db.query(m.ReviewItem).filter_by(code="actual_shortage", status="open").count()
        == 1
    )


@pytest.mark.parametrize("supported", [False, True])
def test_incoming_member_loan_cash_origin_is_separate(correction_pool, supported):
    from app.portfolio.observation_state import observation_state

    db, holder, bank, (unknown, second, client) = correction_pool
    owner = db.query(m.Party).filter_by(entity_id=holder).one().id
    own = run(
        db,
        "create_fund",
        code="OWN",
        holder_entity_id=holder,
        beneficiary_id=owner,
        purpose="general",
    )["fund_id"]
    lender = run(db, "create_party", code="JOSEPH", name="Joseph")["party_id"]
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": 50000}])[0]
    run(db, "post_bank", observation_id=obs, fund_id=unknown, rationale="Statement")
    review = db.query(m.ReviewItem).filter_by(observation_id=obs, code="suspense").one()
    data = dict(
        observation={
            "observation_id": obs,
            "review_id": review.id,
            "expected_state_digest": observation_state(db, obs)["state_digest"],
        },
        party_id=lender,
        code="JOSEPH-500",
        kind="party_payable",
        date="2026-02-01",
        evidence="Confirmed loan",
    )
    if supported:
        data.update(
            source_own_fund_id=own,
            lender_source_evidence="Lender and company loan source confirmed",
        )
    result = run(db, "correct_posted_party_balance", **data)
    assert balance(db, control(db, holder, "payable"), item="JOSEPH-500") == -50000
    assert balance(db, bank, fund_id=own) == (50000 if supported else 0)
    assert len(result["open_funding_review_ids"]) == (0 if supported else 1)
    assert result["review_lifecycle"]["net_open_reduction"] == (1 if supported else 0)
    if not supported:
        resolution = dict(
            review_id=result["open_funding_review_ids"][0],
            observation_id=obs,
            expected_state_digest=observation_state(db, obs)["state_digest"],
            loan_event_id=result["event_id"],
            lender_party_id=lender,
            evidence="Lender confirms this specific incoming loan",
        )
        with pytest.raises(Problem, match="lender"):
            run(
                db,
                "resolve_loan_funding_source",
                **{**resolution, "lender_party_id": owner},
            )
        event_count = db.query(m.Event).count()
        command = dict(
            operation="resolve_loan_funding_source",
            key=uuid.uuid4().hex,
            data=resolution,
        )
        resolved = execute(db, command)
        assert execute(db, command)["replayed"]
        assert resolved["unrelated_funding_not_certified"]
        assert db.query(m.Event).count() == event_count
        assert balance(db, bank, fund_id=unknown) == 50000


@pytest.mark.parametrize("bad_kind", ["date", "other_observation", "amount"])
def test_legacy_cash_normalizer_rejects_unproven_history(correction_pool, bad_kind):
    db, holder, bank, (unknown, second, client) = correction_pool
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": 240000}])[0]
    original = run(
        db, "post_bank", observation_id=obs, fund_id=unknown, rationale="Statement"
    )
    day = "2026-02-02" if bad_kind == "date" else "2026-02-01"
    source = {"observation_id": obs + 1 if bad_kind == "other_observation" else obs}
    lines = [ln(bank, -240000, fund_id=unknown), ln(bank, 240000, fund_id=second)]
    if bad_kind == "amount":
        lines = [
            ln(bank, 100, fund_id=unknown),
            ln(control(db, holder, "equity"), -100),
        ]
    journal(db, holder, day, lines, corrects_id=original["event_id"], source=source)
    before = db.query(m.Event).count()
    with pytest.raises(Problem):
        run(db, "correct_posted_pool", **pool_correction(db, obs, client))
    assert db.query(m.Event).count() == before
    assert db.query(m.LineAllocation).count() == 0


def test_pool_preview_exposes_lifecycle_and_preserves_state(client, correction_pool):
    db, holder, bank, (unknown, second, fund) = correction_pool
    obs = bank_rows(db, bank, [{"date": "2026-02-01", "amount_cents": 240000}])[0]
    run(db, "post_bank", observation_id=obs, fund_id=unknown, rationale="Statement")
    command = dict(
        operation="correct_posted_pool",
        key=uuid.uuid4().hex,
        data=pool_correction(db, obs, fund),
    )
    before = db.query(m.Event).count()
    preview = client.post("/api/portfolio/proposals", json=command)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    result = body["preview"]["result"]
    assert result["committed"] is False
    assert result["review_lifecycle"]["gross_closures"] == 1
    assert result["affected_fund_scopes"]
    assert db.query(m.Event).count() == before
    assert db.query(m.LineAllocation).count() == 0
    response = client.post(
        f"/api/portfolio/proposals/{body['proposal_id']}/commit",
        json={"digest": body["digest"]},
    )
    assert response.status_code == 200, response.text
    assert balance(db, bank, fund_id=fund) == 240000


def test_legacy_allocation_artifact_requires_relevant_correction_and_coverage(
    correction_pool,
):
    from app.portfolio.assessments import shortage_assessment

    db, holder, bank, (unknown, second, client) = correction_pool
    small, funding, outgoing = bank_rows(
        db,
        bank,
        [
            {"date": "2026-01-01", "amount_cents": 10000},
            {"date": "2026-01-01", "amount_cents": 50000},
            {"date": "2026-02-01", "amount_cents": -30000},
        ],
    )
    first = run(
        db, "post_bank", observation_id=small, fund_id=unknown, rationale="Statement"
    )
    run(
        db,
        "pool_activity",
        observation_id=funding,
        fund_id=client,
        evidence="Client funds",
    )
    original = run(
        db, "post_bank", observation_id=outgoing, fund_id=unknown, rationale="Statement"
    )
    alert = (
        db.query(m.ReviewItem)
        .filter_by(event_id=original["event_id"], code="actual_shortage")
        .one()
    )
    correction = journal(
        db,
        holder,
        "2026-02-01",
        [
            ln(bank, 30000, fund_id=unknown),
            ln(bank, -30000, fund_id=client),
            ln(control(db, holder, "custody_liability"), 30000, fund_id=client),
            ln(control(db, holder, "suspense_debit"), -30000),
        ],
        corrects_id=original["event_id"],
        source={"observation_id": outgoing},
    )
    run(
        db,
        "reconcile",
        account_id=bank,
        start="2026-01-01",
        end="2026-02-28",
        opening_cents=0,
        ending_cents=30000,
    )
    assessment = shortage_assessment(db, alert.id, date(2026, 2, 28))
    data = dict(
        review_id=alert.id,
        as_of="2026-02-28",
        expected_assessment_digest=assessment["assessment_digest"],
        corrective_event_id=correction["event_id"],
        reason="source_allocation_corrected",
        evidence="Client payment confirmed",
        coverage_evidence="Complete supported bank statements",
    )
    with pytest.raises(Problem, match="disproves"):
        run(
            db,
            "resolve_historical_shortage",
            **{**data, "corrective_event_id": first["event_id"]},
        )
    key = uuid.uuid4().hex
    command = dict(operation="resolve_historical_shortage", key=key, data=data)
    assert execute(db, command)["financial_entries_added"] is False
    assert execute(db, command)["replayed"]
