"""Card contracts distinguish source liability, beneficiary cost and bank settlement."""

from datetime import date
from typing import Literal
from pydantic import Field, model_validator
from app.portfolio.schemas import Input, Id, Cents, SignedCents, BankRow, CONTRACTS


class CardIdentityInput(Input):
    code: str = Field(min_length=1, max_length=64)
    account_id: Id
    entity_id: Id
    issuer: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1)


class CardAliasInput(Input):
    card_id: Id
    alias: str = Field(min_length=1, max_length=100)
    start: date
    end: date | None = None
    evidence: str = Field(min_length=1)


class IssuerRow(BankRow):
    external_id: str = Field(min_length=1, max_length=200)
    activity_status: Literal["posted", "pending", "declined"]


class CardImportInput(Input):
    card_id: Id
    alias: str = Field(min_length=1, max_length=100)
    source_name: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rows: list[IssuerRow] = Field(min_length=1, max_length=20000)


class CardObservation(Input):
    observation_id: Id
    expected_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CardCostComponent(Input):
    key: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    amount_cents: Cents = Field(gt=0)
    beneficiary_kind: Literal["company", "entity", "external_party"]
    entity_id: Id | None = None
    party_id: Id | None = None
    account_id: Id | None = None
    property_id: Id | None = None
    unit_id: Id | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def target(self):
        if self.beneficiary_kind == "external_party":
            if not self.party_id or self.entity_id or self.account_id:
                raise ValueError(
                    "External beneficiaries require only a party identity; no invented external books"
                )
        elif (
            not self.account_id
            or self.party_id
            or (self.beneficiary_kind == "entity" and not self.entity_id)
        ):
            raise ValueError(
                "Company/entity cost requires its real cost account and maintained entity identity"
            )
        return self


class CardPurchaseInput(Input):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    card_id: Id
    observation: CardObservation
    components: list[CardCostComponent] = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1)


class CardRefundComponent(Input):
    key: str = Field(min_length=1, max_length=24)
    amount_cents: Cents = Field(gt=0)


class CardRefundInput(Input):
    code: str = Field(min_length=1, max_length=64)
    card_id: Id
    observation: CardObservation
    purchase_id: Id
    components: list[CardRefundComponent] = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1)


class CardPaymentInput(Input):
    code: str = Field(min_length=1, max_length=64)
    card_id: Id
    bank_observation: CardObservation
    issuer_observation: CardObservation
    bank_fund_id: Id | None = None
    item: str = Field(min_length=1, max_length=100)
    retry_of_return_id: Id | None = None
    evidence: str = Field(min_length=1)


class CardPaymentReturnInput(CardPaymentInput):
    original_payment_id: Id


class CardCoverageInput(Input):
    card_id: Id
    expected_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    supersedes_id: Id | None = None
    start: date
    end: date
    opening_liability_cents: SignedCents
    ending_liability_cents: SignedCents
    observation_ids: list[Id] = Field(max_length=20000)
    document_id: Id
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: str = Field(min_length=1)


CARD_CONTRACTS = {
    "register_card_identity": CardIdentityInput,
    "add_card_alias": CardAliasInput,
    "import_card_activity": CardImportInput,
    "classify_card_purchase": CardPurchaseInput,
    "record_card_refund": CardRefundInput,
    "match_card_repayment": CardPaymentInput,
    "return_card_repayment": CardPaymentReturnInput,
    "record_card_coverage": CardCoverageInput,
}

# Register after construction so either schema module may be imported first.
CONTRACTS.update(CARD_CONTRACTS)
