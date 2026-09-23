"""Bind browser sessions to their authenticated team."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "0002_session_team_scope"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {column["name"] for column in inspect(bind).get_columns("sessions")}


def _indexes(bind) -> set[str]:
    return {index["name"] for index in inspect(bind).get_indexes("sessions")}


def upgrade() -> None:
    bind = op.get_bind()
    if "team_id" not in _columns(bind):
        op.add_column(
            "sessions",
            sa.Column("team_id", sa.String(length=32), sa.ForeignKey("teams.id"), nullable=True),
        )
        # A legacy user can theoretically have no membership due to an
        # interrupted/manual import. Leave that session null and let auth fail
        # closed instead of inventing a tenant.
        bind.execute(
            text(
                "UPDATE sessions AS s "
                "SET team_id = (SELECT m.team_id FROM memberships AS m "
                "WHERE m.user_id = s.user_id ORDER BY m.id LIMIT 1)"
            )
        )
    if "ix_sessions_team_id" not in _indexes(bind):
        op.create_index("ix_sessions_team_id", "sessions", ["team_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_sessions_team_id" in _indexes(bind):
        op.drop_index("ix_sessions_team_id", table_name="sessions")
    if "team_id" in _columns(bind):
        op.drop_column("sessions", "team_id")
