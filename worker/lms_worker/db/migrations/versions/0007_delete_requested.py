"""a request to delete an assignment's LMS submission

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('assignments', sa.Column('delete_requested_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('assignments', 'delete_requested_at')
