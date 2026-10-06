"""status tracking, papers and chat

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('chat_sessions',
    sa.Column('chat_id', sa.String(length=64), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('chat_id')
    )
    op.create_table('inbound_messages',
    sa.Column('message_id', sa.String(length=128), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('message_id')
    )
    op.add_column('assignments', sa.Column('kind', sa.String(length=16), server_default='assignment', nullable=False))
    op.add_column('assignments', sa.Column('term', sa.String(length=32), nullable=True))
    op.add_column('assignments', sa.Column('submitted_file', sa.String(length=512), nullable=True))
    op.add_column('assignments', sa.Column('deadline_passed', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('assignments', sa.Column('marks', sa.String(length=64), nullable=True))
    op.add_column('assignments', sa.Column('status_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('assignments', sa.Column('question_path', sa.String(length=1024), nullable=True))
    op.add_column('assignments', sa.Column('announced_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('assignments', sa.Column('reminders', sa.String(length=64), server_default='', nullable=False))
    op.create_index(op.f('ix_assignments_kind'), 'assignments', ['kind'], unique=False)
    op.add_column('jobs', sa.Column('note', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'note')
    op.drop_index(op.f('ix_assignments_kind'), table_name='assignments')
    op.drop_column('assignments', 'reminders')
    op.drop_column('assignments', 'announced_at')
    op.drop_column('assignments', 'question_path')
    op.drop_column('assignments', 'status_seen_at')
    op.drop_column('assignments', 'marks')
    op.drop_column('assignments', 'deadline_passed')
    op.drop_column('assignments', 'submitted_file')
    op.drop_column('assignments', 'term')
    op.drop_column('assignments', 'kind')
    op.drop_table('inbound_messages')
    op.drop_table('chat_sessions')
