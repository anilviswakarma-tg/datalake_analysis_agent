"""Chat history storage: which database, its schema, and what a saved answer
carries so it can be rebuilt later.

Backend-agnostic by construction. The database is chosen by one SQLAlchemy
URL (CHAT_DB_URL) - SQLite on a volume today, Postgres or anything else
SQLAlchemy supports later - and the schema below uses only portable types,
so the same tables are created on any of them. Nothing here imports a UI
framework; the Chainlit adapter lives in chainlit_data.py.

Moving to a real database is a configuration change:
    CHAT_DB_URL=postgresql+asyncpg://user:pass@host/db   (+ pip install asyncpg)
Existing SQLite history does not move by itself; copy it across if it matters.
"""

from __future__ import annotations

import functools
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from config import DATA_DIR

DEFAULT_SQLITE_FILE = DATA_DIR / "chat_history.db"
PREVIEW_ROWS = 100      # the rows the app already holds in memory for a result


# ═══════════════════════════════════════════════════════════════════════════
# WHICH DATABASE
# ═══════════════════════════════════════════════════════════════════════════

def chat_db_url() -> Optional[str]:
    """The history database URL, or None when history is switched off.

    CHAT_DB_URL unset -> SQLite at data/chat_history.db.
    CHAT_DB_URL=off   -> no history (conversations live only in memory).
    """
    raw = os.getenv("CHAT_DB_URL", "").strip()
    if raw.lower() in ("off", "none", "false"):
        return None
    return raw or f"sqlite+aiosqlite:///{DEFAULT_SQLITE_FILE.as_posix()}"


def is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def connect_args(url: str) -> Dict[str, Any]:
    """Driver options. SQLite gets a busy timeout so two writers queue
    instead of failing with 'database is locked'."""
    return {"timeout": 15} if is_sqlite(url) else {}


# ═══════════════════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════════════════
# Chainlit's SQLAlchemy data layer writes to these tables with raw SQL, so
# table and column names must match what it emits, quoted camelCase included.
# Deliberately NOT Chainlit's published DDL, which is Postgres-only (UUID,
# JSONB, TEXT[]) and misses "autoCollapse" - a column every tool step writes,
# so each insert failed, silently. Portable types instead: ids and JSON are
# TEXT (Chainlit sends ids as strings and JSON already serialised).
# tests/test_chat_store.py checks every field Chainlit writes has a column.

SCHEMA = sa.MetaData()

_id = sa.Text
_json = sa.Text

sa.Table(
    "users", SCHEMA,
    sa.Column("id", _id, primary_key=True),
    sa.Column("identifier", sa.Text, nullable=False, unique=True),
    sa.Column("metadata", _json, nullable=False),
    sa.Column("createdAt", sa.Text),
)

sa.Table(
    "threads", SCHEMA,
    sa.Column("id", _id, primary_key=True),
    sa.Column("createdAt", sa.Text),
    sa.Column("name", sa.Text),
    sa.Column("userId", _id, sa.ForeignKey("users.id", ondelete="CASCADE")),
    sa.Column("userIdentifier", sa.Text),
    sa.Column("tags", _json),
    sa.Column("metadata", _json),
)

sa.Table(
    "steps", SCHEMA,
    sa.Column("id", _id, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("threadId", _id, sa.ForeignKey("threads.id", ondelete="CASCADE"),
              nullable=False, index=True),
    sa.Column("parentId", _id),
    sa.Column("streaming", sa.Boolean, nullable=False),
    sa.Column("waitForAnswer", sa.Boolean),
    sa.Column("isError", sa.Boolean),
    sa.Column("metadata", _json),
    sa.Column("tags", _json),
    sa.Column("input", sa.Text),
    sa.Column("output", sa.Text),
    sa.Column("createdAt", sa.Text),
    sa.Column("command", sa.Text),
    sa.Column("start", sa.Text),
    sa.Column("end", sa.Text),
    sa.Column("generation", _json),
    sa.Column("showInput", sa.Text),
    sa.Column("language", sa.Text),
    sa.Column("indent", sa.Integer),
    sa.Column("defaultOpen", sa.Boolean),
    sa.Column("autoCollapse", sa.Boolean),
    sa.Column("modes", _json),
)

sa.Table(
    "elements", SCHEMA,
    sa.Column("id", _id, primary_key=True),
    sa.Column("threadId", _id, sa.ForeignKey("threads.id", ondelete="CASCADE"), index=True),
    sa.Column("type", sa.Text),
    sa.Column("url", sa.Text),
    sa.Column("chainlitKey", sa.Text),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("display", sa.Text),
    sa.Column("objectKey", sa.Text),
    sa.Column("size", sa.Text),
    sa.Column("page", sa.Integer),
    sa.Column("language", sa.Text),
    sa.Column("forId", _id),
    sa.Column("mime", sa.Text),
    sa.Column("props", _json),
)

sa.Table(
    "feedbacks", SCHEMA,
    sa.Column("id", _id, primary_key=True),
    sa.Column("forId", _id, nullable=False),
    sa.Column("threadId", _id, sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=False),
    sa.Column("value", sa.Integer, nullable=False),
    sa.Column("comment", sa.Text),
)


# Ours, not Chainlit's: which chats a user has saved from the retention
# sweep. A table of its own rather than a flag in threads.metadata, so the
# sweep can filter on it in plain SQL on any backend, and create_all adds it
# to an existing database.
sa.Table(
    "saved_threads", SCHEMA,
    sa.Column("threadId", _id, sa.ForeignKey("threads.id", ondelete="CASCADE"),
              primary_key=True),
    sa.Column("userIdentifier", sa.Text, nullable=False, index=True),
    sa.Column("savedAt", sa.Text, nullable=False),
)


async def ensure_schema(url: str) -> None:
    """Create any missing tables. Idempotent, and additive only: it never
    alters an existing table, so a column added in a Chainlit upgrade needs
    an ALTER TABLE by hand (check the release notes when upgrading)."""
    if is_sqlite(url):
        database = make_url(url).database
        if database and database != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(database)), exist_ok=True)
    engine = create_async_engine(url, connect_args=connect_args(url))
    try:
        async with engine.begin() as conn:
            if is_sqlite(url):
                # WAL lets the UI read history while an answer is being saved.
                # It is a property of the file, so setting it once persists.
                await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.run_sync(SCHEMA.create_all)
    finally:
        await engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════
# SAVED CHATS AND RETENTION
# ═══════════════════════════════════════════════════════════════════════════
# A chat nobody has saved is deleted once it has had no activity for
# RETENTION_DAYS (CHAT_RETENTION_DAYS, default 60; 0 keeps everything).
# Activity is the newest message, so a chat that is still being used is
# never swept, however old it is.

def retention_days() -> int:
    try:
        return max(0, int(os.getenv("CHAT_RETENTION_DAYS", "60")))
    except ValueError:
        return 60


def _iso(dt: datetime) -> str:
    """The timestamp format Chainlit writes (chainlit.utils.utc_now), so
    timestamps compare correctly as text."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


@functools.lru_cache(maxsize=None)
def _engine(url: str):
    return create_async_engine(url, connect_args=connect_args(url))


def _t(name: str) -> sa.Table:
    return SCHEMA.tables[name]


async def set_saved(url: str, thread_id: str, user_identifier: str, saved: bool) -> None:
    table = _t("saved_threads")
    async with _engine(url).begin() as conn:
        await conn.execute(table.delete().where(table.c.threadId == thread_id))
        if saved:
            await conn.execute(table.insert().values(
                threadId=thread_id, userIdentifier=user_identifier,
                savedAt=_iso(datetime.now(timezone.utc))))


async def saved_thread_ids(url: str, user_identifier: str) -> Set[str]:
    table = _t("saved_threads")
    async with _engine(url).connect() as conn:
        rows = await conn.execute(sa.select(table.c.threadId)
                                  .where(table.c.userIdentifier == user_identifier))
        return {r[0] for r in rows}


async def purge_expired(url: str, days: Optional[int] = None,
                        now: Optional[datetime] = None) -> int:
    """Delete unsaved chats idle for longer than `days`. Returns how many."""
    days = retention_days() if days is None else days
    if days <= 0:
        return 0
    cutoff = _iso((now or datetime.now(timezone.utc)) - timedelta(days=days))
    threads, steps, saved = _t("threads"), _t("steps"), _t("saved_threads")
    last = (sa.select(steps.c.threadId, sa.func.max(steps.c.createdAt).label("last"))
            .group_by(steps.c.threadId).subquery())
    expired = (sa.select(threads.c.id)
               .outerjoin(last, last.c.threadId == threads.c.id)
               .where(sa.func.coalesce(last.c.last, threads.c.createdAt) < cutoff)
               .where(threads.c.id.not_in(sa.select(saved.c.threadId))))
    async with _engine(url).begin() as conn:
        ids = [r[0] for r in await conn.execute(expired)]
        if ids:
            await _delete_threads(conn, ids)
    return len(ids)


async def _delete_threads(conn, ids: Iterable[str]) -> None:
    # Children first, explicitly: SQLite ignores ON DELETE CASCADE unless
    # foreign keys are switched on for the connection.
    ids = list(ids)
    for name in ("feedbacks", "elements", "steps", "saved_threads"):
        table = _t(name)
        await conn.execute(table.delete().where(table.c.threadId.in_(ids)))
    await conn.execute(_t("threads").delete().where(_t("threads").c.id.in_(ids)))


# ═══════════════════════════════════════════════════════════════════════════
# WHAT A SAVED ANSWER CARRIES
# ═══════════════════════════════════════════════════════════════════════════
# Results are stored inline with the answer, not as files: the ~100 preview
# rows the app already holds, the chart spec and the Athena query id. That is
# a few KB, keeps history independent of any blob store, and lets a reopened
# chat rebuild its table and chart. The full CSV is re-fetched from S3 by
# query id while Athena's result object still exists.

SNAPSHOT_KEY = "datalake"


def answer_metadata(question: str, answer: str, df: Optional[pd.DataFrame],
                    query_id: Optional[str], chart: Optional[Dict[str, Any]],
                    notices: List[str]) -> Dict[str, Any]:
    """The metadata to store on an answer message. JSON-safe."""
    result = None
    if df is not None:
        preview = df.head(PREVIEW_ROWS)
        result = {
            "query_id": query_id,
            "chart": chart,
            "rows": len(df),
            # to_json handles numpy types, NaN and timestamps; split keeps order
            "preview": json.loads(preview.to_json(orient="split", index=False,
                                                  date_format="iso")),
        }
    return {SNAPSHOT_KEY: {"v": 1, "question": question, "answer": answer,
                           "notices": list(notices), "result": result}}


def read_answer_metadata(metadata: Any) -> Optional[Dict[str, Any]]:
    """The saved-answer record from a step's metadata, or None. Accepts the
    JSON string the database returns as well as a dict."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return None
    if not isinstance(metadata, dict):
        return None
    record = metadata.get(SNAPSHOT_KEY)
    return record if isinstance(record, dict) else None


def preview_dataframe(result: Optional[Dict[str, Any]]) -> Optional[pd.DataFrame]:
    """Rebuild the stored preview rows as a DataFrame."""
    preview = (result or {}).get("preview")
    if not preview:
        return None
    return pd.DataFrame(preview.get("data", []), columns=preview.get("columns", []))
