"""Shared fixtures for the integration suite.

Deliberately uses a REAL, ephemeral Postgres and Redis rather than mocks or SQLite:
this app's schema relies on a Postgres-native `Sequence`/`nextval()` (see
app/models.py - it's how the id-before-insert Base62 strategy works), so anything
other than real Postgres would either fail outright or silently test different
behavior than production. Both servers are started once per test session and torn
down at the end - the ~1-2s Postgres `initdb`/startup cost is paid once for the
whole suite, not per test.

CRITICAL ORDERING NOTE: the environment variables below MUST be set before any
`app.*` module is imported anywhere in the test session, because `app.config.Settings`
is instantiated once at import time as a module-level singleton. Pytest always fully
executes the rootdir's conftest.py (this file) before collecting/importing any
test_*.py module, so setting them here - before any `from app...` import in this
file - is what guarantees every test, in every file, sees these values.
"""
import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import pytest

_TEST_PG_PORT = 55432
_TEST_REDIS_PORT = 55379

os.environ["DATABASE_URL"] = f"postgresql+asyncpg://postgres@localhost:{_TEST_PG_PORT}/url_shortener_test"
os.environ["REDIS_URL"] = f"redis://localhost:{_TEST_REDIS_PORT}/0"
# High enough that the ordinary functional tests never trip it by accident; the
# dedicated rate-limit tests lower this via monkeypatch for their own duration and
# use a unique fake client IP so they don't consume any other test's quota.
os.environ["RATE_LIMIT_PER_IP_PER_MINUTE"] = "100000"
os.environ["RATE_LIMIT_PER_SHORTCODE_PER_MINUTE"] = "100000"
# Flush near-instantly instead of the 1s production default, so the analytics
# integration test doesn't need a slow sleep to observe the counter update.
os.environ["ANALYTICS_FLUSH_INTERVAL_SECONDS"] = "0.1"
os.environ["ANALYTICS_FLUSH_BATCH_SIZE"] = "50"
# See app.db's comment on this flag: the suite mixes a sync TestClient (its own
# internal event loop) with a couple of async httpx.AsyncClient-based tests (their
# own per-test loops) against one SQLAlchemy async engine. NullPool means every DB
# checkout is a fresh asyncpg connection bound to whichever loop is currently
# running, so no connection ever gets handed across a loop boundary.
os.environ["DATABASE_USE_NULL_POOL"] = "true"
os.environ["REDIS_DISABLE_SHARED_POOL"] = "true"

from fastapi.testclient import TestClient  # noqa: E402 (must follow the env var setup above)


def _wait_for_port(port: str | int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("localhost", int(port))) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"Nothing listening on localhost:{port} after {timeout}s")


@pytest.fixture(scope="session")
def postgres_server():
    data_dir = tempfile.mkdtemp(prefix="url_shortener_test_pg_")
    subprocess.run(
        ["initdb", "-D", data_dir, "-U", "postgres", "--auth=trust", "--no-sync"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        [
            "pg_ctl", "-D", data_dir, "-w",
            "-o", f"-p {_TEST_PG_PORT} -k {data_dir} -h localhost",
            "-l", os.path.join(data_dir, "server.log"),
            "start",
        ],
        check=True, capture_output=True, text=True,
    )
    _wait_for_port(_TEST_PG_PORT)
    subprocess.run(
        ["createdb", "-h", "localhost", "-p", str(_TEST_PG_PORT), "-U", "postgres", "url_shortener_test"],
        check=True, capture_output=True, text=True,
    )

    yield

    subprocess.run(["pg_ctl", "-D", data_dir, "-m", "immediate", "stop"], capture_output=True)
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def redis_server():
    pid_file = tempfile.mktemp(suffix=".pid")
    log_file = tempfile.mktemp(suffix=".log")
    subprocess.run(
        ["redis-server", "--port", str(_TEST_REDIS_PORT), "--daemonize", "yes",
         "--pidfile", pid_file, "--logfile", log_file],
        check=True, capture_output=True, text=True,
    )
    _wait_for_port(_TEST_REDIS_PORT)

    yield

    subprocess.run(["redis-cli", "-p", str(_TEST_REDIS_PORT), "shutdown", "nosave"], capture_output=True)


@pytest.fixture(scope="session")
def db_schema(postgres_server):
    """Creates the schema once for the session and drops it at the end.

    Deliberately a plain sync fixture (not an async generator) driven by its own
    `asyncio.run()` calls, rather than a pytest-asyncio-managed async fixture: with
    NullPool (see app.db) every connection is short-lived and self-contained, so
    there's no need for this setup/teardown to share an event loop with the tests
    that follow - and *not* sharing one sidesteps a pytest-asyncio fixture-scope vs.
    test-loop-scope mismatch entirely. Imported lazily (inside the fixture, not at
    module top) so it only happens after the env vars above are already in place.
    """
    from app.db import Base, engine

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _drop() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    asyncio.run(_create())

    yield

    asyncio.run(_drop())


@pytest.fixture(scope="session")
def client(db_schema, redis_server):
    """A single TestClient for the whole session, so the app's lifespan (and the
    background analytics worker it starts) runs once for all integration tests,
    the same way it would run once for a real deployment.
    """
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def unique_ip() -> str:
    """A fresh fake client IP per test, so rate-limit tests that intentionally
    lower the limit never share a Redis token-bucket key with any other test.
    """
    return f"203.0.113.{uuid.uuid4().int % 254 + 1}"
