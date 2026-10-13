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
