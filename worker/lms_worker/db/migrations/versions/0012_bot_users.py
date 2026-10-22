"""people who use the public menu in groups and private chats

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'bot_users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('chat_id', sa.String(length=64), nullable=False),
        sa.Column('person', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('requests', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_option', sa.String(length=256), nullable=False, server_default=''),
        sa.Column('first_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('chat_id', 'person'),
    )
    op.create_index('ix_bot_users_chat_id', 'bot_users', ['chat_id'])


def downgrade() -> None:
    op.drop_index('ix_bot_users_chat_id', table_name='bot_users')
    op.drop_table('bot_users')
