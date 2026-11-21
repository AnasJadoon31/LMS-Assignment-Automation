"""courses are known by semester and course name: the LMS now encrypts its ids, a new value on every page

Codes stay as they were, so assignment refs, fingerprints and file folders do not change.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-22
"""

from alembic import op


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ('courses', 'class_courses'):
        op.execute(f"UPDATE {table} SET semester = semester_name, lms_ref = name WHERE semester_name <> ''")
    op.execute("UPDATE classes SET semester = semester_name WHERE semester_name <> ''")


def downgrade() -> None:
    pass  # the old base64 ids no longer work on the LMS
