import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

_engine = None
_Session = None
_migration_thread_lock = Lock()
MIGRATION_LOCK_ID = 0x534F435241544442


def database_url() -> str:
    return os.environ["DATABASE_URL"]


def engine():
    global _engine, _Session
    if _engine is None:
        url = database_url()
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, **kwargs)
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _alembic_ini() -> Path:
    override = os.environ.get("ALEMBIC_INI")
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise RuntimeError(f"ALEMBIC_INI does not exist: {override}")
    candidates = [
        Path.cwd() / "alembic.ini",
        Path("/app/alembic.ini"),
        Path(__file__).resolve().parents[2] / "alembic.ini",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("alembic.ini not found")


def init_schema() -> None:
    from alembic.config import Config

    from alembic import command

    ini = _alembic_ini()
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
    # Alembic's module proxy is not thread-safe; PostgreSQL session advisory ownership
    # also serializes independent bot/broker processes before version-table inspection.
    with _migration_thread_lock:
        migration_engine = create_engine(database_url(), poolclass=NullPool)
        try:
            with migration_engine.connect() as connection, migration_lock(connection):
                cfg.attributes["connection"] = connection
                command.upgrade(cfg, "head")
        finally:
            migration_engine.dispose()


@contextmanager
def migration_lock(connection):
    postgres = connection.dialect.name == "postgresql"
    if postgres:
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_ID}
        )
        connection.commit()  # session-level lock survives migration transactions
    try:
        yield
    finally:
        if postgres:
            if connection.in_transaction():
                connection.rollback()
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_ID}
            )
            connection.commit()


@contextmanager
def session() -> Iterator[Session]:
    engine()
    factory = _Session
    if factory is None:
        raise RuntimeError("database session is not configured")
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
