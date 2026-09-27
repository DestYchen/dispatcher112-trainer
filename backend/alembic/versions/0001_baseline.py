"""Empty baseline: Alembic owns schema versioning from the first startup."""

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.get_context().impl.static_output("-- Baseline registered; no application tables yet.")


def downgrade() -> None:
    op.get_context().impl.static_output("-- Baseline removed; no application tables changed.")
