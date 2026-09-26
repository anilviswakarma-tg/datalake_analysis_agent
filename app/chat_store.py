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

import json
import os
from typing import Any, Dict, List, Optional

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
