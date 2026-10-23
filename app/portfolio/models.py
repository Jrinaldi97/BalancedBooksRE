"""Portfolio dimensions and controls. Financial amounts live in transaction_lines."""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    Boolean,
    ForeignKey,
    JSON,
    UniqueConstraint,
    CheckConstraint,
    func,
)
from app.database import Base


class Entity(Base):
    __tablename__ = "re_entities"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    kind = Column(String(30), nullable=False, default="llc")
    closed_through = Column(Date)
    revision = Column(Integer, nullable=False, default=0)


class Property(Base):
    __tablename__ = "re_properties"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    address = Column(Text)


class Unit(Base):
    __tablename__ = "re_units"
    __table_args__ = (UniqueConstraint("property_id", "code"),)
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("re_properties.id"), nullable=False)
    code = Column(String(32), nullable=False)


class Party(Base):
    __tablename__ = "re_parties"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    kind = Column(String(30), nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), unique=True)


class Ownership(Base):
    __tablename__ = "re_ownership"
    __table_args__ = (CheckConstraint("share_ppm >= 0 AND share_ppm <= 1000000"),)
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("re_parties.id"), nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"))
    property_id = Column(Integer, ForeignKey("re_properties.id"))
    unit_id = Column(Integer, ForeignKey("re_units.id"))
    share_ppm = Column(Integer)
    interest = Column(String(30), nullable=False, default="economic")
    start = Column(Date, nullable=False)
    end = Column(Date)
    evidence = Column(Text, nullable=False)


class AccountScope(Base):
    __tablename__ = "re_account_scopes"
    __table_args__ = (UniqueConstraint("entity_id", "code"),)
    account_id = Column(Integer, ForeignKey("accounts.id"), primary_key=True)
    entity_id = Column(
        Integer, ForeignKey("re_entities.id"), nullable=False, index=True
    )
    code = Column(String(32), nullable=False)
    role = Column(String(40), nullable=False)
    cash_flow = Column(String(20), nullable=False, default="operating")
    pooled = Column(Boolean, nullable=False, default=False)
    coverage_start = Column(Date)


class Fund(Base):
    __tablename__ = "re_funds"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    holder_entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)
    beneficiary_id = Column(Integer, ForeignKey("re_parties.id"), nullable=False)
    property_id = Column(Integer, ForeignKey("re_properties.id"))
    unit_id = Column(Integer, ForeignKey("re_units.id"))
    purpose = Column(String(40), nullable=False)
    restriction = Column(String(100), nullable=False, default="unrestricted")


class Event(Base):
    __tablename__ = "re_events"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    kind = Column(String(40), nullable=False)
    date = Column(Date, nullable=False, index=True)
    description = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    actor = Column(String(200), nullable=False)
    source = Column(JSON, nullable=False, default=dict)
    corrects_id = Column(Integer, ForeignKey("re_events.id"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class JournalScope(Base):
    __tablename__ = "re_journal_scopes"
    transaction_id = Column(Integer, ForeignKey("transactions.id"), primary_key=True)
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False, index=True)
    entity_id = Column(
        Integer, ForeignKey("re_entities.id"), nullable=False, index=True
    )


class LineScope(Base):
    __tablename__ = "re_line_scopes"
    line_id = Column(Integer, ForeignKey("transaction_lines.id"), primary_key=True)
    property_id = Column(Integer, ForeignKey("re_properties.id"), index=True)
    unit_id = Column(Integer, ForeignKey("re_units.id"))
    fund_id = Column(Integer, ForeignKey("re_funds.id"), index=True)
    counterparty_id = Column(Integer, ForeignKey("re_entities.id"))
    item = Column(String(100), index=True)
    cash_flow = Column(String(20))


class BankObservation(Base):
    __tablename__ = "re_bank_observations"
    __table_args__ = (UniqueConstraint("account_id", "source_key"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    description = Column(Text, nullable=False)
    reference = Column(String(200))
    source_key = Column(String(200), nullable=False)
    fingerprint = Column(String(64), nullable=False, index=True)
    source = Column(JSON, nullable=False)
    event_id = Column(Integer, ForeignKey("re_events.id"))
    status = Column(String(30), nullable=False, default="imported")


class ReviewItem(Base):
    __tablename__ = "re_review_items"
    id = Column(Integer, primary_key=True)
    code = Column(String(50), nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"))
    event_id = Column(Integer, ForeignKey("re_events.id"))
    observation_id = Column(Integer, ForeignKey("re_bank_observations.id"))
    detail = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="open")
    resolution = Column(Text)
    resolved_event_id = Column(Integer, ForeignKey("re_events.id"))
    created_at = Column(DateTime, server_default=func.now())


class Document(Base):
    __tablename__ = "re_documents"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    title = Column(String(300), nullable=False)
    provider_id = Column(String(300))


class DocumentLocation(Base):
    __tablename__ = "re_document_locations"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("re_documents.id"), nullable=False)
    locator = Column(Text, nullable=False)
    sha256 = Column(String(64))
    created_at = Column(DateTime, server_default=func.now())


class DocumentLink(Base):
    __tablename__ = "re_document_links"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("re_documents.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)
    page = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())


class RequestRecord(Base):
    __tablename__ = "re_requests"
    key = Column(String(120), primary_key=True)
    digest = Column(String(64), nullable=False)
    operation = Column(String(60), nullable=False)
    result = Column(JSON, nullable=False)
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class Proposal(Base):
    __tablename__ = "re_proposals"
    id = Column(Integer, primary_key=True)
    request = Column(JSON, nullable=False)
    digest = Column(String(64), nullable=False)
    revisions = Column(JSON, nullable=False)
    preview = Column(JSON, nullable=False)
    committed_result = Column(JSON)


class BasisAdjustment(Base):
    __tablename__ = "re_basis_adjustments"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)
    date = Column(Date, nullable=False, index=True)
    reason = Column(Text, nullable=False)
    lines = Column(JSON, nullable=False)


class ManagementAllocation(Base):
    __tablename__ = "re_management_allocations"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)
    property_id = Column(Integer, ForeignKey("re_properties.id"))
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    date = Column(Date, nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)


class OpenItem(Base):
    __tablename__ = "re_open_items"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)
    property_id = Column(Integer, ForeignKey("re_properties.id"))
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)
    kind = Column(String(30), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    control_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)


class ItemApplication(Base):
    __tablename__ = "re_item_applications"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("re_open_items.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)
    amount_cents = Column(Integer, nullable=False)


class Reconciliation(Base):
    __tablename__ = "re_reconciliations"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    start = Column(Date, nullable=False)
    end = Column(Date, nullable=False)
    opening_cents = Column(Integer, nullable=False)
    ending_cents = Column(Integer, nullable=False)
    document_id = Column(Integer, ForeignKey("re_documents.id"))
    result = Column(JSON, nullable=False)
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class ReconciliationRevision(Base):
    """An immutable reviewed replacement, never a rewrite of a bank snapshot."""

    __tablename__ = "re_reconciliation_revisions"
    __table_args__ = (CheckConstraint("supersedes_id <> replacement_id"),)
    id = Column(Integer, primary_key=True)
    supersedes_id = Column(
        Integer, ForeignKey("re_reconciliations.id"), unique=True, nullable=False
    )
    replacement_id = Column(
        Integer, ForeignKey("re_reconciliations.id"), unique=True, nullable=False
    )
    reason = Column(Text, nullable=False)
    source_documents = Column(JSON, nullable=False)
    previous_digest = Column(String(64), nullable=False)
    actor = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class Schedule(Base):
    __tablename__ = "re_schedules"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)
    property_id = Column(Integer, ForeignKey("re_properties.id"))
    kind = Column(String(40), nullable=False)
    description = Column(Text, nullable=False)
    inputs = Column(JSON, nullable=False)
    supersedes_id = Column(Integer, ForeignKey("re_schedules.id"))
    evidence = Column(Text, nullable=False)


class SchedulePosting(Base):
    __tablename__ = "re_schedule_postings"
    __table_args__ = (UniqueConstraint("schedule_id", "line_key"),)
    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey("re_schedules.id"), nullable=False)
    line_key = Column(String(100), nullable=False)
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)


class ReportRun(Base):
    __tablename__ = "re_report_runs"
    id = Column(Integer, primary_key=True)
    parameters = Column(JSON, nullable=False)
    result = Column(JSON, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class CashClassification(Base):
    __tablename__ = "re_cash_classifications"
    id = Column(Integer, primary_key=True)
    line_id = Column(
        Integer, ForeignKey("transaction_lines.id"), nullable=False, index=True
    )
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False)
    date = Column(Date, nullable=False)
    cash_flow = Column(String(20), nullable=False)
    reason = Column(Text, nullable=False)


class ReportingGroup(Base):
    __tablename__ = "re_reporting_groups"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    start = Column(Date, nullable=False)
    end = Column(Date)
    control_assessment = Column(Text, nullable=False)
    reviewed_by = Column(String(200), nullable=False)
    evidence = Column(Text, nullable=False)


class GroupMember(Base):
    __tablename__ = "re_group_members"
    __table_args__ = (UniqueConstraint("group_id", "entity_id"),)
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("re_reporting_groups.id"), nullable=False)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)


class GroupAdjustment(Base):
    __tablename__ = "re_group_adjustments"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("re_reporting_groups.id"), nullable=False)
    date = Column(Date, nullable=False)
    basis = Column(String(40), nullable=False)
    lines = Column(JSON, nullable=False)
    reason = Column(Text, nullable=False)
    evidence = Column(Text, nullable=False)
    reviewed_by = Column(String(200), nullable=False)
    cash_flow_event_ids = Column(JSON, nullable=False, default=list)


class ReportingPolicy(Base):
    __tablename__ = "re_reporting_policies"
    id = Column(Integer, primary_key=True)
    entity_id = Column(Integer, ForeignKey("re_entities.id"), nullable=False)
    topic = Column(String(100), nullable=False)
    start = Column(Date, nullable=False)
    end = Column(Date)
    guidance = Column(Text, nullable=False)
    treatment = Column(Text, nullable=False)
    assumptions = Column(JSON, nullable=False)
    reviewed_by = Column(String(200), nullable=False)
    evidence = Column(Text, nullable=False)
    supersedes_id = Column(Integer, ForeignKey("re_reporting_policies.id"))


class LineAllocation(Base):
    """Append-only replacement scope components; original journal stays intact."""

    __tablename__ = "re_line_allocations"
    id = Column(Integer, primary_key=True)
    line_id = Column(
        Integer, ForeignKey("transaction_lines.id"), nullable=False, index=True
    )
    event_id = Column(Integer, ForeignKey("re_events.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    components = Column(JSON, nullable=False)
    evidence = Column(Text, nullable=False)
    supersedes_id = Column(Integer, ForeignKey("re_line_allocations.id"))
