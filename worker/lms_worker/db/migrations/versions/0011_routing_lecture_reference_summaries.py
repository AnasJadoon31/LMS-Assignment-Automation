"""Haiku's triage per job, lecture notes as a reference per course, lecture summaries

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('courses', sa.Column('lectures', sa.String(length=16), server_default='off', nullable=False))
    op.add_column('jobs', sa.Column('triage', sa.Text(), nullable=True))
    op.create_table(
        'summaries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('course_id', sa.Integer(), sa.ForeignKey('courses.id'), nullable=False),
        sa.Column('lectures', sa.Text(), nullable=False),
        sa.Column('style', sa.String(length=16), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('triage', sa.Text(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('not_before', sa.DateTime(timezone=True), nullable=True),
        sa.Column('claude_session', sa.String(length=64), nullable=True),
        sa.Column('cost_tokens', sa.BigInteger(), nullable=False),
        sa.Column('path', sa.String(length=1024), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_summaries_course_id', 'summaries', ['course_id'])
    op.create_index('ix_summaries_state', 'summaries', ['state'])


def downgrade() -> None:
    op.drop_table('summaries')
    op.drop_column('jobs', 'triage')
    op.drop_column('courses', 'lectures')
