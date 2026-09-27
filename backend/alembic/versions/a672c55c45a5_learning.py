"""learning"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a672c55c45a5"
down_revision: str | None = "778405d8f101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learning_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_learning_groups_teacher_id"), "learning_groups", ["teacher_id"], unique=False
    )
    op.create_table(
        "learning_materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_learning_materials_teacher_id"), "learning_materials", ["teacher_id"], unique=False
    )
    op.create_table(
        "learning_models",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("dataset_kind", sa.String(length=16), nullable=False),
        sa.Column("dataset_note", sa.Text(), nullable=False),
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("teacher_id", "version"),
    )
    op.create_index(
        op.f("ix_learning_models_teacher_id"), "learning_models", ["teacher_id"], unique=False
    )
    op.create_table(
        "learning_modules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.Integer(), nullable=False),
        sa.Column("material_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scenario_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_learning_modules_teacher_id"), "learning_modules", ["teacher_id"], unique=False
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["learning_groups.id"],
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("group_id", "student_id"),
    )
    op.create_index(
        op.f("ix_group_members_student_id"), "group_members", ["student_id"], unique=False
    )
    op.create_table(
        "learning_forecasts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["learning_models.id"],
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_learning_forecasts_student_id"), "learning_forecasts", ["student_id"], unique=False
    )
    op.create_index(
        op.f("ix_learning_forecasts_teacher_id"), "learning_forecasts", ["teacher_id"], unique=False
    )
    op.create_table(
        "module_assignments",
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["learning_groups.id"],
        ),
        sa.ForeignKeyConstraint(
            ["module_id"],
            ["learning_modules.id"],
        ),
        sa.PrimaryKeyConstraint("module_id", "group_id"),
    )
    op.create_table(
        "module_progress",
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["module_id"],
            ["learning_modules.id"],
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("module_id", "student_id"),
    )
    op.create_table(
        "assignment_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["assignments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assignment_feedback_assignment_id"),
        "assignment_feedback",
        ["assignment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_assignment_feedback_assignment_id"), table_name="assignment_feedback")
    op.drop_table("assignment_feedback")
    op.drop_table("module_progress")
    op.drop_table("module_assignments")
    op.drop_index(op.f("ix_learning_forecasts_teacher_id"), table_name="learning_forecasts")
    op.drop_index(op.f("ix_learning_forecasts_student_id"), table_name="learning_forecasts")
    op.drop_table("learning_forecasts")
    op.drop_index(op.f("ix_group_members_student_id"), table_name="group_members")
    op.drop_table("group_members")
    op.drop_index(op.f("ix_learning_modules_teacher_id"), table_name="learning_modules")
    op.drop_table("learning_modules")
    op.drop_index(op.f("ix_learning_models_teacher_id"), table_name="learning_models")
    op.drop_table("learning_models")
    op.drop_index(op.f("ix_learning_materials_teacher_id"), table_name="learning_materials")
    op.drop_table("learning_materials")
    op.drop_index(op.f("ix_learning_groups_teacher_id"), table_name="learning_groups")
    op.drop_table("learning_groups")
