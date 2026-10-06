"""lab paper exam windows

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('assignments', sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('assignments', sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('assignments', sa.Column('submission_hidden', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('assignments', 'submission_hidden')
    op.drop_column('assignments', 'opened_at')
    op.drop_column('assignments', 'starts_at')
