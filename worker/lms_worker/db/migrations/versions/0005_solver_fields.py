"""solver fields on jobs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('not_before', sa.DateTime(timezone=True), nullable=True))
    op.add_column('jobs', sa.Column('claude_session', sa.String(length=64), nullable=True))
    op.add_column('jobs', sa.Column('cost_usd', sa.Float(), server_default='0', nullable=False))
    op.add_column('jobs', sa.Column('summary', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'summary')
    op.drop_column('jobs', 'cost_usd')
    op.drop_column('jobs', 'claude_session')
    op.drop_column('jobs', 'not_before')
