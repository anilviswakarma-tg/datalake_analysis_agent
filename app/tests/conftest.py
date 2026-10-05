"""Put the app directory on sys.path so tests import the modules directly,
matching how Streamlit runs them (app.py's own directory is the import root).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _isolated_run_context():
    """Give every test its own run context and undo any start_run() it makes,
    so one test's model choice or query budget can't leak into the next."""
    import run_state
    token = run_state._CURRENT_RUN.set(run_state.RunContext())
    yield
    run_state._CURRENT_RUN.reset(token)


@pytest.fixture
def db_url(tmp_path):
    """The chat database for a test: a fresh SQLite file by default.

    Set TEST_DATABASE_URL to run the same tests on Postgres. Every table is
    dropped first, so never point it at the app's database; the local Docker
    one has a separate test database for this:
      docker exec datalake-chat-db createdb -U datalake datalake_chat_test
      TEST_DATABASE_URL=postgresql+asyncpg://datalake:datalake_local@localhost:5432/datalake_chat_test"""
    import os
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        yield f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
        return
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine
    import chat_store
    if url == os.getenv("CHAT_DB_URL"):
        pytest.exit("TEST_DATABASE_URL is the app's own database; the tests "
                    "would wipe it. Use a separate test database.")

    async def wipe():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(chat_store.SCHEMA.drop_all)
        await engine.dispose()
    asyncio.run(wipe())
    yield url
