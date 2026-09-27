"""Persist authorized local SIP exercise calls."""

import sqlalchemy as sa

from alembic import op

revision = "778405d8f101"
down_revision = "64b374a4f100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sip_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), sa.ForeignKey("assignments.id"), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("callee_code", sa.String(32)),
        sa.Column("media_name", sa.String(64), nullable=False),
        sa.Column("channel_id", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.String(255)),
        sa.CheckConstraint("direction IN ('INBOUND', 'OUTBOUND')"),
        sa.CheckConstraint("state IN ('REQUESTED', 'RINGING', 'CONNECTED', 'ENDED', 'FAILED')"),
    )
    op.create_index(
        "ix_sip_calls_active_user",
        "sip_calls",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('REQUESTED', 'RINGING', 'CONNECTED')"),
    )


def downgrade() -> None:
    op.drop_table("sip_calls")
