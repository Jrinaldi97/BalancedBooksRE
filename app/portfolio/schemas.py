"""Strict, discoverable input contracts shared by UI, CLI and AI tools."""

from datetime import date
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

Id = Annotated[StrictInt, Field(gt=0)]
Cents = Annotated[StrictInt, Field(ge=0, le=99999999999999)]
SignedCents = Annotated[StrictInt, Field(ge=-99999999999999, le=99999999999999)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityInput(Input):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    kind: str = "llc"


class PropertyInput(Input):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None


class UnitInput(Input):
    property_id: Id
    code: str = Field(min_length=1, max_length=32)


class PartyInput(EntityInput):
    entity_id: Id | None = None


class OwnershipInput(Input):
    owner_id: Id
    entity_id: Id | None = None
    property_id: Id | None = None
    unit_id: Id | None = None
    share_ppm: Annotated[StrictInt, Field(ge=0, le=1000000)] | None = None
    interest: Literal["legal", "economic", "voting"] = "economic"
    start: date
    end: date | None = None
    evidence: str = Field(min_length=1)


class AccountInput(Input):
    entity_id: Id
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    account_type: Literal["asset", "liability", "equity", "income", "expense", "cogs"]
    role: Literal[
        "bank",
        "credit_card",
        "cash_equivalent",
        "asset",
        "fixed_asset",
        "project",
        "escrow",
        "receivable",
        "payable",
        "prepaid",
        "deferred",
        "debt",
        "deposit",
        "last_month_rent",
        "due_from",
        "due_to",
        "custody_asset",
        "custody_liability",
        "suspense_debit",
        "suspense_credit",
        "income",
        "expense",
        "cogs",
        "equity",
        "contribution",
        "distribution",
        "contra_asset",
        "bridge",
    ]
    cash_flow: Literal["operating", "investing", "financing"] = "operating"
    pooled: bool = False
    coverage_start: date | None = None


class FundInput(Input):
    code: str = Field(min_length=1, max_length=64)
    holder_entity_id: Id
    beneficiary_id: Id
    property_id: Id | None = None
    unit_id: Id | None = None
    purpose: Literal[
        "general",
        "reserve",
        "security_deposit",
        "last_month_rent",
        "escrow",
        "unresolved",
    ]
    restriction: str = "unrestricted"


class Line(Input):
    account_id: Id
    debit_cents: Cents = 0
    credit_cents: Cents = 0
    property_id: Id | None = None
    unit_id: Id | None = None
    fund_id: Id | None = None
    counterparty_id: Id | None = None
    item: str | None = None
    cash_flow: Literal["operating", "investing", "financing", "internal"] | None = None
    description: str = ""


class Journal(Input):
    entity_id: Id
    lines: list[Line] = Field(min_length=2, max_length=2000)


class Adjustment(Journal):
    date: date
    reason: str = Field(min_length=1)


class ManagementInput(Input):
    entity_id: Id
    property_id: Id | None = None
    account_id: Id
    date: date
    amount_cents: SignedCents


class Posting(Input):
    date: date
    description: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    kind: str = "journal"
    source: dict = Field(default_factory=dict)
    journals: list[Journal] = Field(min_length=1, max_length=100)
    adjustments: list[Adjustment] = Field(default_factory=list)
    management: list[ManagementInput] = Field(default_factory=list)
    corrects_id: Id | None = None
    observed: bool = False
    release_reason: str | None = None


class BankRow(Input):
    physical_row: Id | None = None
    source_sheet: str | None = None
    date: date
    amount_cents: SignedCents
    description: str = ""
    reference: str = ""
    external_id: str | None = None


class ImportInput(Input):
    account_id: Id
    source_name: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rows: list[BankRow] = Field(min_length=1, max_length=20000)


class BankPostInput(Input):
    observation_id: Id
    offset_account_id: Id | None = None
    property_id: Id | None = None
    fund_id: Id | None = None
    rationale: str = Field(min_length=1)
    duplicate_resolution: Literal["distinct"] | None = None


class TransferInput(Input):
    withdrawal_id: Id
    deposit_id: Id
    purpose: Literal["internal", "advance", "repayment"]
    item: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1)
    source_property_id: Id | None = None
    destination_property_id: Id | None = None
    source_fund_id: Id | None = None
    destination_fund_id: Id | None = None


class AccrualInput(Input):
    code: str = Field(min_length=1, max_length=64)
    entity_id: Id
    property_id: Id | None = None
    date: date
    kind: Literal["expense", "income", "asset"]
    account_id: Id
    control_id: Id
    amount_cents: Annotated[StrictInt, Field(gt=0, le=99999999999999)]
    description: str
    evidence: str = Field(min_length=1)


class SettleInput(Input):
    item_id: Id
    observation_id: Id
    fund_id: Id | None = None
    evidence: str = Field(min_length=1)


class CorrectionInput(Input):
    review_id: Id
    account_id: Id
    property_id: Id | None = None
    evidence: str = Field(min_length=1)
    date: date


class DocumentInput(Input):
    code: str = Field(min_length=1, max_length=64)
    title: str
    locator: str = Field(min_length=1)
    provider_id: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    event_id: Id | None = None
    page: str | None = None


class ReconcileInput(Input):
    account_id: Id
    start: date
    end: date
    opening_cents: SignedCents
    ending_cents: SignedCents
    document_id: Id | None = None


class PeriodInput(Input):
    entity_id: Id
    through: date
    reason: str = Field(min_length=1)


class ScheduleInput(Input):
    code: str
    entity_id: Id
    property_id: Id | None = None
    kind: Literal[
        "asset",
        "development",
        "closing",
        "lease",
        "debt",
        "revenue",
        "commission",
        "allowance",
        "contingency",
        "equity",
        "consolidation",
    ]
    description: str
    inputs: dict
    evidence: str = Field(min_length=1)
    supersedes_id: Id | None = None


class SchedulePostInput(Input):
    schedule_id: Id
    line_key: str
    posting: Posting


class Command(Input):
    operation: str
    key: str = Field(min_length=1, max_length=120)
    data: dict


CONTRACTS = {
    "create_entity": EntityInput,
    "create_property": PropertyInput,
    "create_unit": UnitInput,
    "create_party": PartyInput,
    "record_ownership": OwnershipInput,
    "create_account": AccountInput,
    "create_fund": FundInput,
    "post": Posting,
    "import_bank": ImportInput,
    "post_bank": BankPostInput,
    "match_transfer": TransferInput,
    "recognize": AccrualInput,
    "settle": SettleInput,
    "correct_suspense": CorrectionInput,
    "attach_document": DocumentInput,
    "reconcile": ReconcileInput,
    "close_period": PeriodInput,
    "reopen_period": PeriodInput,
    "create_schedule": ScheduleInput,
    "post_schedule": SchedulePostInput,
}


class DuplicateInput(Input):
    observation_id: Id
    original_id: Id
    evidence: str = Field(min_length=1)


CONTRACTS["resolve_duplicate"] = DuplicateInput


class PoolActivityInput(Input):
    observation_id: Id
    fund_id: Id
    offset_account_id: Id | None = None
    property_id: Id | None = None
    advance_from_fund_id: Id | None = None
    advance_item: str | None = None
    release_reason: str | None = None
    evidence: str = Field(min_length=1)


class PostedPoolInput(PoolActivityInput):
    review_id: Id
    expected_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


CONTRACTS["correct_posted_pool"] = PostedPoolInput


CONTRACTS["pool_activity"] = PoolActivityInput


class DebtPaymentInput(Input):
    observation_id: Id
    debt_account_id: Id
    interest_account_id: Id | None = None
    escrow_account_id: Id | None = None
    fee_account_id: Id | None = None
    principal_cents: Cents = 0
    interest_cents: Cents = 0
    escrow_cents: Cents = 0
    fee_cents: Cents = 0
    property_id: Id | None = None
    instrument: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class SegmentTransferInput(Input):
    entity_id: Id
    date: date
    account_id: Id
    source_property_id: Id
    destination_property_id: Id
    amount_cents: Annotated[StrictInt, Field(gt=0)]
    evidence: str = Field(min_length=1)


class DepreciationInput(Input):
    schedule_id: Id
    date: date
    amount_cents: Annotated[StrictInt, Field(gt=0)]
    expense_account_id: Id
    accumulated_account_id: Id
    evidence: str = Field(min_length=1)


class ProjectReleaseInput(Input):
    entity_id: Id
    date: date
    project_account_id: Id
    cost_of_sales_account_id: Id
    property_id: Id
    amount_cents: Annotated[StrictInt, Field(gt=0)]
    project_item: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


CONTRACTS.update(
    mortgage_payment=DebtPaymentInput,
    transfer_property_allocation=SegmentTransferInput,
    depreciate=DepreciationInput,
    release_project_cost=ProjectReleaseInput,
)


class ServiceTimingInput(Input):
    observation_id: Id
    balance_account_id: Id
    result_account_id: Id
    property_id: Id | None = None
    item: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class ServiceEarnInput(Input):
    entity_id: Id
    date: date
    balance_account_id: Id
    result_account_id: Id
    property_id: Id | None = None
    item: str = Field(min_length=1)
    amount_cents: Annotated[StrictInt, Field(gt=0)]
    evidence: str = Field(min_length=1)


CONTRACTS.update(
    service_payment=ServiceTimingInput, service_recognition=ServiceEarnInput
)


class OwnershipEndInput(Input):
    ownership_id: Id
    end: date
    evidence: str = Field(min_length=1)


class ClosingInput(Input):
    observation_id: Id
    schedule_id: Id
    lines: list[Line] = Field(min_length=2, max_length=2000)
    evidence: str = Field(min_length=1)


class ReviewResolveInput(Input):
    corrective_event_id: Id | None = None
    review_id: Id
    evidence: str = Field(min_length=1)


CONTRACTS.update(
    end_ownership=OwnershipEndInput,
    gross_closing=ClosingInput,
    resolve_review=ReviewResolveInput,
)

CONTRACTS["return_settlement"] = SettleInput


class GroupInput(Input):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    entity_ids: list[Id] = Field(min_length=1)
    start: date
    end: date | None = None
    control_assessment: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class GroupAdjustmentInput(Input):
    cash_flow_event_ids: list[Id] = Field(default_factory=list)
    group_id: Id
    date: date
    basis: Literal["both", "accrual", "modified_cash_bank_posting"] = "both"
    lines: list[Line] = Field(min_length=2)
    reason: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)


class PolicyInput(Input):
    entity_id: Id
    topic: str = Field(min_length=1)
    start: date
    end: date | None = None
    guidance: str = Field(min_length=1)
    treatment: str = Field(min_length=1)
    assumptions: dict = Field(default_factory=dict)
    reviewed_by: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    supersedes_id: Id | None = None


CONTRACTS.update(
    create_reporting_group=GroupInput,
    group_adjustment=GroupAdjustmentInput,
    record_policy=PolicyInput,
)


class BatchOperation(Input):
    operation: str
    data: dict


class BatchInput(Input):
    operations: list[BatchOperation] = Field(min_length=1, max_length=200)


CONTRACTS["batch"] = BatchInput


class PoolCorrectionComponent(PoolActivityInput):
    amount_cents: SignedCents


class SplitPoolCorrectionInput(Input):
    observation_id: Id
    review_id: Id
    expected_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: str = Field(min_length=1)
    components: list[PoolCorrectionComponent] = Field(min_length=1, max_length=100)


CONTRACTS["correct_posted_pool_split"] = SplitPoolCorrectionInput


class PostedSettlementInput(SettleInput):
    review_id: Id
    expected_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


CONTRACTS["correct_posted_settlement"] = PostedSettlementInput


class PostedTransferLeg(Input):
    observation_id: Id
    review_id: Id
    expected_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class PostedInternalTransferInput(Input):
    withdrawal: PostedTransferLeg
    deposit: PostedTransferLeg
    fund_id: Id
    property_id: Id | None = None
    evidence: str = Field(min_length=1)
    release_reason: str | None = None


CONTRACTS["correct_posted_internal_transfer"] = PostedInternalTransferInput


class PostedIntercompanyInput(Input):
    withdrawal: PostedTransferLeg
    deposit: PostedTransferLeg
    purpose: Literal["advance", "repayment"]
    item: str = Field(min_length=1, max_length=100)
    date: date
    evidence: str = Field(min_length=1)


CONTRACTS["correct_posted_intercompany"] = PostedIntercompanyInput


class PostedPartyBalanceInput(Input):
    source_own_fund_id: Id | None = None
    lender_source_evidence: str | None = Field(default=None, min_length=1)
    observation: PostedTransferLeg
    party_id: Id
    code: str = Field(min_length=1, max_length=64)
    kind: Literal["party_receivable", "party_payable"]
    date: date
    property_id: Id | None = None
    evidence: str = Field(min_length=1)


CONTRACTS["correct_posted_party_balance"] = PostedPartyBalanceInput


class ExpectedCorrectionBalance(Input):
    account_id: Id
    entity_id: Id | None = None
    amount_cents: SignedCents
    property_id: Id | None = None
    unit_id: Id | None = None
    fund_id: Id | None = None
    counterparty_id: Id | None = None
    item: str | None = None
    party_id: Id | None = None
    cash_flow: Literal["operating", "investing", "financing", "internal"] | None = None


class CorrectionSourceMovement(Input):
    observation_id: Id
    account_id: Id
    date: date
    amount_cents: SignedCents


class CorrectionMovementManifest(Input):
    kind: Literal["single_bank", "paired_bank", "card_liability"]
    observations: list[CorrectionSourceMovement] = Field(min_length=1, max_length=2)


class ValidateExistingCorrectionInput(Input):
    observation: PostedTransferLeg
    corrective_event_id: Id
    balances: list[ExpectedCorrectionBalance] = Field(min_length=1, max_length=100)
    movement_manifest: CorrectionMovementManifest | None = None
    evidence: str = Field(min_length=1)


CONTRACTS["validate_existing_correction"] = ValidateExistingCorrectionInput


class HistoricalShortageResolution(Input):
    review_id: Id
    expected_assessment_digest: str = Field(min_length=1)
    as_of: date
    corrective_event_id: Id | None = None
    corrective_event_ids: list[Id] | None = Field(
        default=None, min_length=1, max_length=100
    )
    reason: Literal[
        "source_allocation_corrected",
        "source_allocation_set_corrected",
        "import_order_artifact",
        "supported_restoration",
    ]
    evidence: str = Field(min_length=1)
    coverage_evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def correction_variant(self):
        if self.reason == "source_allocation_set_corrected":
            if self.corrective_event_id is not None or not self.corrective_event_ids:
                raise ValueError(
                    "The correction-set variant requires only corrective_event_ids"
                )
            if len(set(self.corrective_event_ids)) != len(self.corrective_event_ids):
                raise ValueError("Correction event identities must be unique")
            self.corrective_event_ids = sorted(self.corrective_event_ids)
        elif self.corrective_event_id is None or self.corrective_event_ids is not None:
            raise ValueError("This variant requires only corrective_event_id")
        return self


CONTRACTS["resolve_historical_shortage"] = HistoricalShortageResolution


class ReconciliationSource(Input):
    document_id: Id
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReconciliationRevisionInput(Input):
    supersedes_id: Id
    expected_reconciliation_digest: str = Field(min_length=1)
    opening_cents: SignedCents
    ending_cents: SignedCents
    observation_ids: list[Id] = Field(max_length=10000)
    source_documents: list[ReconciliationSource] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1)


CONTRACTS["revise_reconciliation"] = ReconciliationRevisionInput


class LoanFundingResolution(Input):
    review_id: Id
    observation_id: Id
    expected_state_digest: str = Field(min_length=1)
    loan_event_id: Id
    lender_party_id: Id
    evidence: str = Field(min_length=1)


CONTRACTS["resolve_loan_funding_source"] = LoanFundingResolution


class FundingComponent(Input):
    source_fund_id: Id | None = None
    own_fund_id: Id | None = None
    amount_cents: Cents = Field(gt=0)
    evidence: str = Field(min_length=1)


class OutgoingFundingResolution(Input):
    review_id: Id
    observation_id: Id
    obligation_event_id: Id
    party_id: Id | None = None
    counterparty_entity_id: Id | None = None
    item: str = Field(min_length=1, max_length=100)
    expected_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_funding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    components: list[FundingComponent] = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1)


CONTRACTS["resolve_outgoing_funding_source"] = OutgoingFundingResolution


from app.portfolio.operations import OperationVerificationInput  # noqa: E402

CONTRACTS["record_operation_verification"] = OperationVerificationInput


class OwnerDistributionShare(Input):
    party_id: Id
    fund_id: Id
    ownership_id: Id
    share_ppm: Annotated[StrictInt, Field(gt=0, le=1000000)]
    amount_cents: Cents


class PostedOwnerDistributionInput(Input):
    mode: Literal["paired", "receipt_only"]
    receipt: PostedTransferLeg
    withdrawal: PostedTransferLeg | None = None
    sender_entity_id: Id
    holder_entity_id: Id
    distribution_date: date
    distribution_account_id: Id | None = None
    custody_account_id: Id
    sender_own_fund_id: Id | None = None
    code: str = Field(min_length=1, max_length=40)
    beneficiaries: list[OwnerDistributionShare] = Field(min_length=1, max_length=100)
    source_evidence: str = Field(min_length=1)
    funding_evidence: str = Field(min_length=1)
    coverage_gap: str | None = Field(default=None, min_length=1)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def source_variant(self):
        if self.mode == "paired":
            if (
                self.withdrawal is None
                or self.distribution_account_id is None
                or self.coverage_gap is not None
            ):
                raise ValueError(
                    "Paired mode needs the actual withdrawal and distribution account, without a coverage gap"
                )
        elif (
            self.withdrawal is not None
            or self.distribution_account_id is not None
            or self.sender_own_fund_id is not None
            or not self.coverage_gap
        ):
            raise ValueError(
                "Receipt-only mode needs an explicit coverage gap and must not declare a sender posting"
            )
        return self


CONTRACTS["correct_posted_owner_distribution_to_holder"] = PostedOwnerDistributionInput


class OwnerCustodyPayoutComponent(Input):
    item_id: Id
    party_id: Id
    fund_id: Id
    amount_cents: Cents = Field(gt=0)
    expected_item_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    recipient_evidence: str = Field(min_length=1)


class PostedOwnerCustodyPayoutInput(Input):
    observation: PostedTransferLeg
    components: list[OwnerCustodyPayoutComponent] = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1)


CONTRACTS["correct_posted_owner_custody_payout"] = PostedOwnerCustodyPayoutInput
# Import the module for registration; direct card_schemas imports work as well.
from app.portfolio import card_schemas as card_schemas  # noqa: E402
