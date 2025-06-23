"""initial schema with complete audit table

Revision ID: 001_initial
Revises: 
Create Date: 2025-06-23 12:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### Create complete audits table with all fields ###
    op.create_table('audits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('report_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('user_id', sa.String(), nullable=True),
        sa.Column('user_audit_report_request_id', sa.String(), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('technical_error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audits_id'), 'audits', ['id'], unique=False)
    op.create_index(op.f('ix_audits_url'), 'audits', ['url'], unique=False)
    op.create_index(op.f('ix_audits_user_id'), 'audits', ['user_id'], unique=False)
    op.create_index(op.f('ix_audits_user_audit_report_request_id'), 'audits', ['user_audit_report_request_id'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### Drop complete audits table ###
    op.drop_index(op.f('ix_audits_user_audit_report_request_id'), table_name='audits')
    op.drop_index(op.f('ix_audits_user_id'), table_name='audits')
    op.drop_index(op.f('ix_audits_url'), table_name='audits')
    op.drop_index(op.f('ix_audits_id'), table_name='audits')
    op.drop_table('audits')
    # ### end Alembic commands ### 