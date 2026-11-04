"""Upstream SPI contract tests use a fresh database so immutable episodes survive no prior test."""

import os

import pytest
from sqlalchemy import create_engine, text

from socrat_bot import db as database
from socrat_bot.models import Base


@pytest.fixture(autouse=True)
def isolated_spi_contract_database(request, tmp_path, monkeypatch):
    if request.node.module.__name__ != "test_spi_card_links":
        yield
        return
    url = (
        os.environ.get("TEST_POSTGRES_URL")
        or f"sqlite+pysqlite:///{tmp_path / 'spi_test.db'}"
    )
    assert "test" in url, "SPI contract tests require a disposable test database"
    cleanup_engine = create_engine(url)
    if cleanup_engine.dialect.name == "postgresql":
        Base.metadata.drop_all(cleanup_engine)
        with cleanup_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_Session", None)
    yield
    if database._engine is not None:
        database._engine.dispose()
    if cleanup_engine.dialect.name == "postgresql":
        Base.metadata.drop_all(cleanup_engine)
        with cleanup_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    cleanup_engine.dispose()
