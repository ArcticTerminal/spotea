"""Pytest fixtures.

Isolation from the real database is the whole point of this file: the app
was already hit by an accidental full data loss once (empty `content`/
`feeds` tables, no backup) — a test run must never be able to touch
`data/spotifrei.db` or `data/storage`/`data/avatars`, even indirectly.

`app/config.py`'s `Settings()` is a module-level singleton instantiated the
first time `app.config` is imported, so the env vars below MUST be set
before any `from app...` import anywhere in the test session — hence they
run at import time, at the very top of this file, before the `from app...`
imports later on.
"""

import os
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="spotifrei-test-"))

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DIR / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_TEST_DIR / "storage")
os.environ["AVATARS_DIR"] = str(_TEST_DIR / "avatars")
os.environ["APP_PASSWORD"] = "test-password"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-use"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.main import app


def _assert_paths_isolated() -> None:
    """A guard that runs once, at collection time (conftest.py itself is
    never collected as a test module by pytest) — a misconfigured
    environment fails loudly here instead of silently touching real data."""
    assert str(_TEST_DIR) in settings.database_url
    assert str(_TEST_DIR) in str(settings.storage_dir)
    assert str(_TEST_DIR) in str(settings.avatars_dir)


_assert_paths_isolated()


@pytest.fixture
def db_session() -> Session:
    """A fresh session per test for seeding data directly. Function-scoped
    (not shared across tests) so a session's SQLAlchemy identity map never
    goes stale from another test's cleanup deleting rows out from under it
    on a different connection."""
    with SessionLocal() as session:
        yield session


@pytest.fixture(scope="session", autouse=True)
def _init_schema():
    """Runs the app's real startup path once (Base.metadata.create_all +
    run_migrations + default user) against the isolated test DB, the same
    way `client` would, so `db_session`-only tests still get a real schema
    without needing to spin up a TestClient themselves."""
    with TestClient(app):
        pass
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_init_schema):
    """Runs after every test — deletes all rows except `users` (the single
    default user every test implicitly relies on existing, same as the app
    itself always assumes) so tests don't leak state into each other
    without paying to recreate the DB file each time."""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "users":
                continue
            conn.execute(table.delete())


@pytest.fixture
def client() -> TestClient:
    """An authenticated TestClient — logs in for real via POST /login, so
    the returned client's cookie jar carries a genuine session and most
    tests never need to think about auth. See test_auth.py for the
    unauthenticated case."""
    with TestClient(app) as c:
        res = c.post("/login", data={"password": settings.app_password})
        assert res.status_code == 200
        yield c
