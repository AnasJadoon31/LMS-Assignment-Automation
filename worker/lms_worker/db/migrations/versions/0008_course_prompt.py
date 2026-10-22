"""a course's own system prompt

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('courses', sa.Column('prompt', sa.Text(), nullable=True))
    op.add_column('courses', sa.Column('prompt_replaces', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('courses', 'prompt_replaces')
    op.drop_column('courses', 'prompt')
