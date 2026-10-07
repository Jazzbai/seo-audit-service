"""Preserve archived sites while excluding them from active work."""

from alembic import op
import sqlalchemy as sa

revision = "0003_site_archive"
down_revision = "0002_session_team_scope"
branch_labels = None
depends_on = None


def upgrade():
    # The initial migration creates tables from current model metadata.
    if "archived_at" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("sites")}:
        op.add_column("sites", sa.Column("archived_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("sites", "archived_at")
