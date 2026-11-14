"""The connection pool (app.db.engine_options): sized by DB_POOL_* on a server database, left alone on SQLite."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine

from app.db import engine_options
from app.settings import Settings


def settings(url: str, **over) -> SimpleNamespace:
    values = {"database_url": url, "db_pool_size": 20, "db_pool_max_overflow": 20, "db_pool_timeout_seconds": 30}
    return SimpleNamespace(**{**values, **over})


def test_a_server_database_gets_the_configured_pool():
    options = engine_options(settings("postgresql+psycopg://localhost/jobhunt", db_pool_size=12,
                                      db_pool_max_overflow=7, db_pool_timeout_seconds=9))
    engine = create_engine("postgresql+psycopg://localhost/jobhunt", **options)  # makes no connection
    assert (engine.pool.size(), engine.pool._max_overflow, engine.pool._timeout) == (12, 7, 9)
    assert options["pool_pre_ping"] is True


def test_the_default_pool_covers_the_workers_that_run_at_once():
    """A worker holds one connection through its model call and needs a second to record the answer, so a pool no
    larger than the worker concurrency deadlocks until the timeout (the stall this guards against)."""
    defaults = Settings(_env_file=None)
    workers = sum(value for name, value in defaults.model_dump().items()
                  if name.endswith("_concurrency") and isinstance(value, int))
    assert defaults.db_pool_size + defaults.db_pool_max_overflow > workers


def test_nonsense_values_still_make_a_usable_pool():
    options = engine_options(settings("postgresql+psycopg://localhost/jobhunt", db_pool_size=0,
                                      db_pool_max_overflow=-3, db_pool_timeout_seconds=0))
    assert (options["pool_size"], options["max_overflow"], options["pool_timeout"]) == (1, 0, 1)


def test_sqlite_keeps_its_own_pool():
    options = engine_options(settings("sqlite:///./jobhunt.db"))
    assert options == {"pool_pre_ping": True, "connect_args": {"check_same_thread": False}}
    create_engine("sqlite://", **options).dispose()
