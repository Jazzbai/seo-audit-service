"""Create the complete ForgeSEO foundation schema.

The first migration intentionally delegates table/index/foreign-key creation to
the checked-in declarative metadata so the isolated SQLite and PostgreSQL
schemas have the same topology.
"""

from __future__ import annotations

from alembic import op

from app.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
