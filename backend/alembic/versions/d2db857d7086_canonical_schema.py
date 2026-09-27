"""canonical_schema"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d2db857d7086"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "directory_entries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("number", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("voice", sa.String(length=32), nullable=False),
        sa.Column("greeting", sa.Text(), server_default=sa.text("'Слушаю вас.'"), nullable=False),
        sa.Column(
            "confirmation",
            sa.Text(),
            server_default=sa.text("'Я вас понял, информация принята.'"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("number"),
    )
    op.create_table(
        "incident_groups",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("short_name", sa.String(length=64), nullable=False),
        sa.Column("is_visible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "streets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_norm", sa.String(length=255), nullable=False),
        sa.Column("district", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_streets_name_norm",
        "streets",
        ["name_norm"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )
    op.create_table(
        "workstations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("number", sa.String(length=16), nullable=False),
        sa.Column("room", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number"),
    )
    op.create_table(
        "incident_types",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("difficulty", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        sa.CheckConstraint("difficulty BETWEEN 1 AND 10"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["incident_groups.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(
        "ix_incident_types_attributes",
        "incident_types",
        ["attributes"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "modifier_services",
        sa.Column(
            "modifier",
            sa.Enum(
                "THREAT_TO_PEOPLE",
                "VICTIMS",
                "FATALITIES",
                "NO_ACCESS",
                "ROAD_BLOCKED",
                "CHILD_INVOLVED",
                name="modifier_code",
            ),
            nullable=False,
        ),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.PrimaryKeyConstraint("modifier", "service_id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("login", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("middle_name", sa.String(length=100), nullable=True),
        sa.Column("role", sa.Enum("ADMIN", "TEACHER", "STUDENT", name="user_role"), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=True),
        sa.Column("totp_secret", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("login"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_log_created_at", "audit_log", [sa.literal_column("created_at DESC")], unique=False
    )
    op.create_index(
        "ix_audit_log_user_created_at",
        "audit_log",
        ["user_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "incident_type_services",
        sa.Column("incident_type_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["incident_type_id"], ["incident_types.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.PrimaryKeyConstraint("incident_type_id", "service_id"),
    )
    op.create_table(
        "lessons",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PLANNED", "RUNNING", "FINISHED", name="lesson_status"),
            server_default=sa.text("'PLANNED'"),
            nullable=False,
        ),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_table(
        "scenarios",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            sa.Enum("TICKET", "GENERATED", "MANUAL", name="scenario_source"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "PENDING_REVIEW", "APPROVED", "REJECTED", name="scenario_status"),
            server_default=sa.text("'DRAFT'"),
            nullable=False,
        ),
        sa.Column(
            "origin",
            sa.Enum("OPERATOR_112", "EXTERNAL_SYSTEM", name="card_origin"),
            server_default=sa.text("'OPERATOR_112'"),
            nullable=False,
        ),
        sa.Column("incident_type_id", sa.Uuid(), nullable=False),
        sa.Column("difficulty", sa.SmallInteger(), nullable=False),
        sa.Column("card_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reference", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("difficulty BETWEEN 1 AND 10"),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["incident_type_id"],
            ["incident_types.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scenarios_status_difficulty", "scenarios", ["status", "difficulty"], unique=False
    )
    op.create_table(
        "assignments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("card_number", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "QUEUED",
                "DELIVERED",
                "OPENED",
                "PRIMARY_SET",
                "CLOSED",
                "EXPIRED",
                name="assignment_state",
            ),
            server_default=sa.text("'QUEUED'"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("primary_status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("score", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("teacher_override", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["scenario_id"],
            ["scenarios.id"],
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assignments_active",
        "assignments",
        ["state"],
        unique=False,
        postgresql_where=sa.text("state IN ('DELIVERED', 'OPENED', 'PRIMARY_SET')"),
    )
    op.create_index(
        "ix_assignments_lesson_student", "assignments", ["lesson_id", "student_id"], unique=False
    )
    op.create_table(
        "lesson_participants",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("workstation_id", sa.Uuid(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workstation_id"],
            ["workstations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lesson_id", "student_id"),
    )
    op.create_table(
        "interaction_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "phone_reports",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("callee_code", sa.String(length=32), nullable=False),
        sa.Column("dialed_number", sa.String(length=16), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "status_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
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
            ),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_automatic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_status_events_assignment_created",
        "status_events",
        ["assignment_id", "created_at"],
        unique=False,
    )
    op.execute("""CREATE FUNCTION forbid_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$""")
    op.execute(
        "CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_immutable ON audit_log")
    op.execute("DROP FUNCTION forbid_audit_mutation()")
    op.drop_index("ix_status_events_assignment_created", table_name="status_events")
    op.drop_table("status_events")
    op.drop_table("phone_reports")
    op.drop_table("interaction_events")
    op.drop_table("lesson_participants")
    op.drop_index("ix_assignments_lesson_student", table_name="assignments")
    op.drop_index(
        "ix_assignments_active",
        table_name="assignments",
        postgresql_where=sa.text("state IN ('DELIVERED', 'OPENED', 'PRIMARY_SET')"),
    )
    op.drop_table("assignments")
    op.drop_index("ix_scenarios_status_difficulty", table_name="scenarios")
    op.drop_table("scenarios")
    op.drop_table("lessons")
    op.drop_table("incident_type_services")
    op.drop_index("ix_audit_log_user_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("users")
    op.drop_table("modifier_services")
    op.drop_index(
        "ix_incident_types_attributes", table_name="incident_types", postgresql_using="gin"
    )
    op.drop_table("incident_types")
    op.drop_table("workstations")
    op.drop_index(
        "ix_streets_name_norm",
        table_name="streets",
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )
    op.drop_table("streets")
    op.drop_table("services")
    op.drop_table("incident_groups")
    op.drop_table("directory_entries")
    for name in (
        "response_status",
        "assignment_state",
        "lesson_status",
        "card_origin",
        "scenario_status",
        "scenario_source",
        "modifier_code",
        "user_role",
    ):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
