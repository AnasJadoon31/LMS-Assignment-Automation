"""Alembic environment. Run through lms_worker.db.upgrade(), which passes the connection."""

from alembic import context

from lms_worker.db.models import Base

target_metadata = Base.metadata
connection = context.config.attributes.get("connection")

if connection is None:
    raise RuntimeError("Run migrations with `lms-worker db-upgrade`, which supplies the database connection")

context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
