from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.learning import AssignmentFeedback as AssignmentFeedback
from app.db.models.learning import GroupMember as GroupMember
from app.db.models.learning import LearningForecast as LearningForecast
from app.db.models.learning import LearningGroup as LearningGroup
from app.db.models.learning import LearningMaterial as LearningMaterial
from app.db.models.learning import LearningModel as LearningModel
from app.db.models.learning import LearningModule as LearningModule
from app.db.models.learning import ModuleAssignment as ModuleAssignment
from app.db.models.learning import ModuleProgress as ModuleProgress
from app.db.models.system import SystemSetting as SystemSetting

user_role = Enum("ADMIN", "TEACHER", "STUDENT", name="user_role")
modifier_code = Enum(
    "THREAT_TO_PEOPLE",
    "VICTIMS",
    "FATALITIES",
    "NO_ACCESS",
    "ROAD_BLOCKED",
    "CHILD_INVOLVED",
    name="modifier_code",
)
scenario_source = Enum("TICKET", "GENERATED", "MANUAL", name="scenario_source")
scenario_status = Enum("DRAFT", "PENDING_REVIEW", "APPROVED", "REJECTED", name="scenario_status")
card_origin = Enum("OPERATOR_112", "EXTERNAL_SYSTEM", name="card_origin")
lesson_status = Enum("PLANNED", "RUNNING", "FINISHED", name="lesson_status")
assignment_state = Enum(
    "QUEUED", "DELIVERED", "OPENED", "PRIMARY_SET", "CLOSED", "EXPIRED", name="assignment_state"
)
response_status = Enum(
    "ADDED",
    "RECEIVED",
    "ACCEPTED",
    "NOT_ACCEPTED",
    "RESPONSE_STARTED",
    "ARRIVED",
    "WORK_IN_PROGRESS",
    "WORK_COMPLETED",
    "WORK_REFUSED",
    name="response_status",
)


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    login: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(user_role, nullable=False)
    service_id: Mapped[UUID | None] = mapped_column(ForeignKey("services.id"))
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), default=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Workstation(Base):
    __tablename__ = "workstations"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    number: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    room: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), default=True, nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[UUID | None] = mapped_column()
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Service(Base):
    __tablename__ = "services"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_visible: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), default=True, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0, nullable=False
    )


class IncidentGroup(Base):
    __tablename__ = "incident_groups"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0, nullable=False
    )


class IncidentType(Base):
    __tablename__ = "incident_types"
    __table_args__ = (CheckConstraint("difficulty BETWEEN 1 AND 10"),)
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(ForeignKey("incident_groups.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    difficulty: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("5"), default=5, nullable=False
    )


class IncidentTypeService(Base):
    __tablename__ = "incident_type_services"
    incident_type_id: Mapped[UUID] = mapped_column(
        ForeignKey("incident_types.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("services.id"), primary_key=True, nullable=False
    )


class ModifierService(Base):
    __tablename__ = "modifier_services"
    modifier: Mapped[str] = mapped_column(modifier_code, primary_key=True, nullable=False)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("services.id"), primary_key=True, nullable=False
    )


class Street(Base):
    __tablename__ = "streets"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    district: Mapped[str | None] = mapped_column(String(128))


class Scenario(Base):
    __tablename__ = "scenarios"
    __table_args__ = (CheckConstraint("difficulty BETWEEN 1 AND 10"),)
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(scenario_source, nullable=False)
    status: Mapped[str] = mapped_column(
        scenario_status, server_default=text("'DRAFT'"), default="DRAFT", nullable=False
    )
    origin: Mapped[str] = mapped_column(
        card_origin, server_default=text("'OPERATOR_112'"), default="OPERATOR_112", nullable=False
    )
    incident_type_id: Mapped[UUID] = mapped_column(ForeignKey("incident_types.id"), nullable=False)
    difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    card_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    author_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Lesson(Base):
    __tablename__ = "lessons"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        lesson_status, server_default=text("'PLANNED'"), default="PLANNED", nullable=False
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LessonParticipant(Base):
    __tablename__ = "lesson_participants"
    __table_args__ = (UniqueConstraint("lesson_id", "student_id"),)
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    lesson_id: Mapped[UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    workstation_id: Mapped[UUID | None] = mapped_column(ForeignKey("workstations.id"))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint("task_mode IN ('CARD_ACTIONS', 'CARD_ENTRY')", name="assignment_task_mode"),
        CheckConstraint("draft_revision >= 0", name="assignment_draft_revision"),
    )
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    lesson_id: Mapped[UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    scenario_id: Mapped[UUID] = mapped_column(ForeignKey("scenarios.id"), nullable=False)
    card_number: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        assignment_state, server_default=text("'QUEUED'"), default="QUEUED", nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    primary_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    teacher_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    task_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="CARD_ACTIONS", server_default=text("'CARD_ACTIONS'")
    )
    card_draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    card_submission: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    draft_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StatusEvent(Base):
    __tablename__ = "status_events"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(response_status, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    is_automatic: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False, nullable=False
    )
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InteractionEvent(Base):
    __tablename__ = "interaction_events"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PhoneReport(Base):
    __tablename__ = "phone_reports"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    )
    callee_code: Mapped[str] = mapped_column(String(32), nullable=False)
    dialed_number: Mapped[str] = mapped_column(String(16), nullable=False)
    transcript: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid()
    )
    lesson_id: Mapped[UUID] = mapped_column(ForeignKey("lessons.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", nullable=False)
    progress: Mapped[float] = mapped_column(default=0.0, nullable=False)
    generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RequestReceipt(Base):
    __tablename__ = "request_receipts"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SipCall(Base):
    __tablename__ = "sip_calls"
    __table_args__ = (
        CheckConstraint("direction IN ('INBOUND', 'OUTBOUND')"),
        CheckConstraint("state IN ('REQUESTED', 'RINGING', 'CONNECTED', 'ENDED', 'FAILED')"),
        Index(
            "ix_sip_calls_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("state IN ('REQUESTED', 'RINGING', 'CONNECTED')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("assignments.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="REQUESTED", nullable=False)
    callee_code: Mapped[str | None] = mapped_column(String(32))
    media_name: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(255))


class DirectoryEntry(Base):
    __tablename__ = "directory_entries"
    id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=func.gen_random_uuid(), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    number: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    voice: Mapped[str] = mapped_column(String(32), nullable=False)
    greeting: Mapped[str] = mapped_column(
        Text, server_default=text("'Слушаю вас.'"), default="Слушаю вас.", nullable=False
    )
    confirmation: Mapped[str] = mapped_column(
        Text,
        server_default=text("'Я вас понял, информация принята.'"),
        default="Я вас понял, информация принята.",
        nullable=False,
    )


Index("ix_audit_log_created_at", AuditLog.created_at.desc())
Index("ix_audit_log_user_created_at", AuditLog.user_id, AuditLog.created_at.desc())
Index("ix_incident_types_attributes", IncidentType.attributes, postgresql_using="gin")
Index(
    "ix_streets_name_norm",
    Street.name_norm,
    postgresql_using="gin",
    postgresql_ops={"name_norm": "gin_trgm_ops"},
)
Index("ix_scenarios_status_difficulty", Scenario.status, Scenario.difficulty)
Index("ix_assignments_lesson_student", Assignment.lesson_id, Assignment.student_id)
Index(
    "ix_assignments_active",
    Assignment.state,
    postgresql_where=Assignment.state.in_(["DELIVERED", "OPENED", "PRIMARY_SET"]),
)
Index("ix_status_events_assignment_created", StatusEvent.assignment_id, StatusEvent.created_at)
