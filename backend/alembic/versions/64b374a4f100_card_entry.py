"""Separate student entry from approved reference cards."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "64b374a4f100"
down_revision: str | None = "52eeaacb9785"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assignments",
        sa.Column("task_mode", sa.String(16), nullable=False, server_default="CARD_ACTIONS"),
    )
    op.add_column("assignments", sa.Column("card_draft", postgresql.JSONB()))
    op.add_column("assignments", sa.Column("card_submission", postgresql.JSONB()))
    op.add_column(
        "assignments", sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("assignments", sa.Column("submitted_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "assignment_task_mode", "assignments", "task_mode IN ('CARD_ACTIONS', 'CARD_ENTRY')"
    )
    op.create_check_constraint("assignment_draft_revision", "assignments", "draft_revision >= 0")


def downgrade() -> None:
    op.drop_constraint("assignment_draft_revision", "assignments", type_="check")
    op.drop_constraint("assignment_task_mode", "assignments", type_="check")
    for name in ("submitted_at", "draft_revision", "card_submission", "card_draft", "task_mode"):
        op.drop_column("assignments", name)
