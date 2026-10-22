"""classes run by their CR: invites, groups, courses, assignments, outbox, log, blocks, usage

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'classes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('paused_by', sa.String(16)),
        sa.Column('muted_until', TS),
        sa.Column('semester', sa.String(64), nullable=False, server_default=''),
        sa.Column('semester_name', sa.String(64), nullable=False, server_default=''),
        sa.Column('enrollment', sa.String(64), nullable=False, server_default=''),
        sa.Column('institute', sa.String(64), nullable=False, server_default='Karachi Campus'),
        sa.Column('password_enc', sa.Text()),
        sa.Column('login_state', sa.String(16), nullable=False, server_default='ok'),
        sa.Column('login_error', sa.Text()),
        sa.Column('rules', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('choices', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('summary_sent', sa.String(32), nullable=False, server_default=''),
        sa.Column('next_poll_at', TS),
        sa.Column('last_poll_at', TS),
        sa.Column('last_error', sa.Text()),
        sa.Column('created_at', TS, nullable=False),
        sa.Column('approved_at', TS),
    )
    op.create_index('ix_classes_status', 'classes', ['status'])
    op.create_table(
        'class_people',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('class_id', sa.Integer(), sa.ForeignKey('classes.id'), nullable=False),
        sa.Column('phone', sa.String(32), nullable=False),
        sa.Column('name', sa.String(128), nullable=False, server_default=''),
        sa.Column('role', sa.String(16), nullable=False, server_default='cr'),
        sa.Column('created_at', TS, nullable=False),
        sa.UniqueConstraint('class_id', 'phone'),
    )
    op.create_index('ix_class_people_class_id', 'class_people', ['class_id'])
    op.create_index('ix_class_people_phone', 'class_people', ['phone'])
    op.create_table(
        'class_invites',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('role', sa.String(16), nullable=False, server_default='cr'),
        sa.Column('class_id', sa.Integer(), sa.ForeignKey('classes.id')),
        sa.Column('created_at', TS, nullable=False),
        sa.Column('expires_at', TS, nullable=False),
        sa.Column('used_at', TS),
        sa.Column('used_by', sa.String(32)),
        sa.Column('revoked', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        'class_groups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('class_id', sa.Integer(), sa.ForeignKey('classes.id'), nullable=False),
        sa.Column('group_jid', sa.String(64), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False, server_default=''),
        sa.Column('added_by', sa.String(32), nullable=False, server_default=''),
        sa.Column('added_at', TS, nullable=False),
    )
    op.create_index('ix_class_groups_class_id', 'class_groups', ['class_id'])
    op.create_table(
        'class_courses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('class_id', sa.Integer(), sa.ForeignKey('classes.id'), nullable=False),
        sa.Column('code', sa.String(64), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('lms_ref', sa.String(64), nullable=False),
        sa.Column('semester', sa.String(64), nullable=False),
        sa.Column('semester_name', sa.String(64), nullable=False, server_default=''),
        sa.Column('last_seen_at', TS, nullable=False),
        sa.UniqueConstraint('class_id', 'semester', 'lms_ref'),
    )
    op.create_index('ix_class_courses_class_id', 'class_courses', ['class_id'])
    op.create_table(
        'class_assignments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('course_id', sa.Integer(), sa.ForeignKey('class_courses.id'), nullable=False),
        sa.Column('lms_ref', sa.String(64), nullable=False),
        sa.Column('number', sa.String(16), nullable=False),
        sa.Column('title', sa.String(512), nullable=False),
        sa.Column('deadline', TS),
        sa.Column('attachment_name', sa.String(512)),
        sa.Column('remarks', sa.Text()),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('deadline_passed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('question_path', sa.String(1024)),
        sa.Column('download_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('reminders', sa.String(64), nullable=False, server_default=''),
        sa.Column('first_seen_at', TS, nullable=False),
        sa.Column('last_seen_at', TS, nullable=False),
        sa.UniqueConstraint('course_id', 'lms_ref'),
    )
    op.create_index('ix_class_assignments_course_id', 'class_assignments', ['course_id'])
    op.create_table(
        'class_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('class_id', sa.Integer(), sa.ForeignKey('classes.id')),
        sa.Column('to', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('file_path', sa.String(1024)),
        sa.Column('created_at', TS, nullable=False),
        sa.Column('sent_at', TS),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text()),
    )
    op.create_index('ix_class_messages_class_id', 'class_messages', ['class_id'])
    op.create_index('ix_class_messages_sent_at', 'class_messages', ['sent_at'])
    op.create_table(
        'class_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('at', TS, nullable=False),
        sa.Column('class_id', sa.Integer(), sa.ForeignKey('classes.id')),
        sa.Column('actor', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''),
        sa.Column('person', sa.String(64)),
    )
    for col in ('at', 'class_id', 'kind', 'person'):
        op.create_index(f'ix_class_log_{col}', 'class_log', [col])
    op.create_table(
        'class_blocks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('class_id', sa.Integer(), sa.ForeignKey('classes.id')),
        sa.Column('person', sa.String(64), nullable=False),
        sa.Column('by', sa.String(16), nullable=False),
        sa.Column('at', TS, nullable=False),
        sa.UniqueConstraint('class_id', 'person'),
    )
    op.create_index('ix_class_blocks_class_id', 'class_blocks', ['class_id'])
    op.create_index('ix_class_blocks_person', 'class_blocks', ['person'])
    op.create_table(
        'class_hits',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('class_id', sa.Integer(), nullable=False),
        sa.Column('who', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('at', TS, nullable=False),
    )
    op.create_index('ix_class_hits_class_id', 'class_hits', ['class_id'])
    op.create_index('ix_class_hits_at', 'class_hits', ['at'])


def downgrade() -> None:
    for t in ('class_hits', 'class_blocks', 'class_log', 'class_messages', 'class_assignments', 'class_courses',
              'class_groups', 'class_invites', 'class_people', 'classes'):
        op.drop_table(t)
