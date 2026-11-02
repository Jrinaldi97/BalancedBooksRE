"""Ordered bank payment rails. No rail is inferred from an accounting category."""

from typing import Literal, get_args

PaymentRail = Literal[
    "Cash",
    "Check",
    "Internal-Bank",
    "ACH",
    "Wire",
    "Zelle",
    "Debit Card",
    "FedNow",
    "RTP",
    "Other/Unknown",
]
PAYMENT_RAILS = list(get_args(PaymentRail))
PAYMENT_RAIL_OPTIONS = [
    {"value": value, "includes": includes}
    for value, includes in zip(
        PAYMENT_RAILS,
        [
            "Currency and coins",
            "Personal checks, business checks, bank/teller/official/certified checks, money orders, or EPOs/ECIs",
            "Book transfers between accounts within the same bank, or bank applied charges/fees",
            "FedACH and EPN; credits, debits, and Same Day ACH",
            "Domestic and international wires; Fedwire, CHIPS, and transfers using SWIFT messaging",
            "Payments between enrolled bank accounts",
            "Purchases, withdrawals, refunds, and transfers using debit-card networks",
            "The Federal Reserve’s instant-payment service",
            "The Clearing House’s instant-payment network",
            "Other payment methods or transactions whose payment method has not been identified",
        ],
        strict=True,
    )
]


def set_payment_rail(db, data, actor):
    from app.portfolio import models as m
    from app.portfolio.service import get, fail

    obs = get(db, m.BankObservation, data.observation_id)
    # Metadata remains editable even for inactive accounts and closed periods.
    account = get(db, m.AccountScope, obs.account_id)
    if account.role != "bank":
        fail("not_bank", "Payment Rail applies only to bank accounts")
    if obs.payment_rail != data.expected_payment_rail:
        fail("stale_payment_rail", "Payment Rail changed; refresh before saving")
    before = obs.payment_rail
    obs.payment_rail = data.payment_rail
    return {
        "observation_id": obs.id,
        "previous_payment_rail": before,
        "payment_rail": obs.payment_rail,
        "evidence": data.evidence,
        "actor": actor,
        "financial_effects": False,
    }


def install_guards(connection):
    from sqlalchemy import inspect

    if not inspect(connection).has_table("re_bank_observations"):
        return
    if "payment_rail" not in {
        c["name"] for c in inspect(connection).get_columns("re_bank_observations")
    }:
        return
    allowed = ",".join("'" + value + "'" for value in PAYMENT_RAILS)
    # Null is accepted only during insertion, then filled for bank accounts.
    for action in ("INSERT", "UPDATE"):
        bank_invalid = f"NEW.payment_rail NOT IN ({allowed})"
        if action == "UPDATE":
            bank_invalid += " OR NEW.payment_rail IS NULL"
        connection.exec_driver_sql(
            f"""CREATE TRIGGER IF NOT EXISTS re_payment_rail_{action}
        BEFORE {action} ON re_bank_observations
        WHEN (EXISTS(SELECT 1 FROM re_account_scopes WHERE account_id=NEW.account_id AND role='bank')
              AND ({bank_invalid}))
          OR (NOT EXISTS(SELECT 1 FROM re_account_scopes WHERE account_id=NEW.account_id AND role='bank')
              AND NEW.payment_rail IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'Payment Rail must use the bank-only ordered taxonomy'); END"""
        )
    connection.exec_driver_sql(
        """CREATE TRIGGER IF NOT EXISTS re_payment_rail_default
    AFTER INSERT ON re_bank_observations
    WHEN NEW.payment_rail IS NULL AND EXISTS(
        SELECT 1 FROM re_account_scopes WHERE account_id=NEW.account_id AND role='bank')
    BEGIN UPDATE re_bank_observations SET payment_rail='Other/Unknown' WHERE id=NEW.id; END"""
    )
