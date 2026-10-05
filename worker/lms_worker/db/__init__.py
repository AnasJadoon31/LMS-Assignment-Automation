from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    TERMINAL_STATES,
    Artifact,
    Assignment,
    Base,
    Course,
    CourseMode,
    Event,
    Job,
    JobState,
    Notification,
    Setting,
)

MIGRATIONS = Path(__file__).parent / "migrations"


def make_engine(url: str) -> Engine:
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return create_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def upgrade(engine: Engine) -> None:
    """Bring the schema to the latest migration."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


__all__ = [
    "TERMINAL_STATES",
    "Artifact",
    "Assignment",
    "Base",
    "Course",
    "CourseMode",
    "Event",
    "Job",
    "JobState",
    "Notification",
    "Setting",
    "make_engine",
    "make_sessionmaker",
    "upgrade",
]
