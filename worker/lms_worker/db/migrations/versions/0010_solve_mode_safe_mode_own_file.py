"""solving mode and safe mode per course, your own file per assignment

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('courses', sa.Column('solve', sa.String(length=16), server_default='bypass', nullable=False))
    op.add_column('courses', sa.Column('safe', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('assignments', sa.Column('own_file', sa.String(length=1024), nullable=True))
    op.add_column('assignments', sa.Column('upload_requested_at', sa.DateTime(timezone=True), nullable=True))
    # notify mode ("only send me the question") is now manual solving
    op.execute("UPDATE jobs SET state = 'WAITING_GO' WHERE state = 'DOWNLOADED' AND assignment_id IN "
               "(SELECT a.id FROM assignments a JOIN courses c ON c.id = a.course_id WHERE c.mode = 'notify')")
    op.execute("UPDATE courses SET solve = 'manual', mode = 'approve' WHERE mode = 'notify'")


def downgrade() -> None:
    op.execute("UPDATE jobs SET state = 'DOWNLOADED' WHERE state IN ('WAITING_GO', 'NEEDS_INPUT')")
    op.drop_column('assignments', 'upload_requested_at')
    op.drop_column('assignments', 'own_file')
    op.drop_column('courses', 'safe')
    op.drop_column('courses', 'solve')
