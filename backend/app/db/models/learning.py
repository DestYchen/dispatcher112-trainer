from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LearningGroup(Base):
    __tablename__ = "learning_groups"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"
    group_id: Mapped[UUID] = mapped_column(ForeignKey("learning_groups.id"), primary_key=True)
    student_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)


class LearningMaterial(Base):
    __tablename__ = "learning_materials"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    difficulty: Mapped[int] = mapped_column(Integer, default=1)
    filename: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(128))
    sha256: Mapped[str | None] = mapped_column(String(64))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LearningModule(Base):
    __tablename__ = "learning_modules"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    instructions: Mapped[str] = mapped_column(Text)
    difficulty: Mapped[int] = mapped_column(Integer)
    material_ids: Mapped[list[str]] = mapped_column(JSONB)
    scenario_ids: Mapped[list[str]] = mapped_column(JSONB)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModuleAssignment(Base):
    __tablename__ = "module_assignments"
    module_id: Mapped[UUID] = mapped_column(ForeignKey("learning_modules.id"), primary_key=True)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("learning_groups.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModuleProgress(Base):
    __tablename__ = "module_progress"
    module_id: Mapped[UUID] = mapped_column(ForeignKey("learning_modules.id"), primary_key=True)
    student_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssignmentFeedback(Base):
    __tablename__ = "assignment_feedback"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("assignments.id"), index=True)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LearningModel(Base):
    __tablename__ = "learning_models"
    __table_args__ = (UniqueConstraint("teacher_id", "version"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[str] = mapped_column(String(64))
    dataset_kind: Mapped[str] = mapped_column(String(16))
    dataset_note: Mapped[str] = mapped_column(Text)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LearningForecast(Base):
    __tablename__ = "learning_forecasts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    model_id: Mapped[UUID] = mapped_column(ForeignKey("learning_models.id"))
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    student_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
