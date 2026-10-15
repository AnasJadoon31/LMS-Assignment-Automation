"""handwritten work waits for approval

Jobs solved for a sheet that asks for handwritten work used to end in HANDWRITTEN and could
never be uploaded. They now wait for approval like any other; move the ones that can still
be uploaded: the latest job of an open assignment with nothing submitted.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    moved = conn.execute(sa.text("""
        UPDATE jobs SET state = 'AWAITING_APPROVAL'
        FROM assignments a
        WHERE jobs.assignment_id = a.id AND jobs.state = 'HANDWRITTEN'
          AND NOT a.deadline_passed AND a.submitted_file IS NULL
          AND jobs.revision = (SELECT max(j.revision) FROM jobs j WHERE j.assignment_id = jobs.assignment_id)
        RETURNING jobs.id
    """)).scalars().all()
    for job_id in moved:
        conn.execute(
            sa.text("INSERT INTO events (job_id, at, from_state, to_state, detail) "
                    "VALUES (:job, now(), 'HANDWRITTEN', 'AWAITING_APPROVAL', :detail)"),
            {"job": job_id, "detail": "handwritten work now waits for your approval"},
        )


def downgrade() -> None:
    pass  # which jobs were moved is in the events; nothing to undo in the schema
