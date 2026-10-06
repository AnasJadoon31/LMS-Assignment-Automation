"""lab paper status from the row colour

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('assignments', sa.Column('paper_status', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('assignments', 'paper_status')
