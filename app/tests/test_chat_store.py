"""Chat history: the portable schema, the data-layer fixes, and the result
snapshot saved with each answer.

Chainlit's SQLAlchemy data layer swallows every database error, so a schema
or type mismatch doesn't fail - history just quietly isn't saved. These tests
drive the real data layer against a real SQLite file to catch that.
"""
import asyncio
import json
import uuid

import numpy as np
import pandas as pd
import pytest
from chainlit.context import init_http_context
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.step import StepDict
from chainlit.user import User

import chat_store
from chainlit_data import build_data_layer


# ── configuration ───────────────────────────────────────────────────────────

def test_default_is_sqlite_in_the_data_dir(monkeypatch):
    monkeypatch.delenv("CHAT_DB_URL", raising=False)
    url = chat_store.chat_db_url()
    assert url.startswith("sqlite+aiosqlite:///")
    assert url.endswith("data/chat_history.db")
    assert chat_store.is_sqlite(url)


@pytest.mark.parametrize("value", ["off", "OFF", "none", "false"])
def test_history_can_be_switched_off(monkeypatch, value):
    monkeypatch.setenv("CHAT_DB_URL", value)
    assert chat_store.chat_db_url() is None


def test_any_sqlalchemy_url_passes_through(monkeypatch):
    """Moving to Postgres is a config change, nothing more."""
    pg = "postgresql+asyncpg://u:p@db:5432/chat"
    monkeypatch.setenv("CHAT_DB_URL", pg)
    assert chat_store.chat_db_url() == pg
    assert not chat_store.is_sqlite(pg)
    assert chat_store.connect_args(pg) == {}


# ── schema ──────────────────────────────────────────────────────────────────

def test_schema_has_a_column_for_every_step_field_chainlit_writes():
    """create_step writes each StepDict key as a column; one missing column
    fails every insert of that kind, silently. `autoCollapse` was missing from
    Chainlit's own published DDL."""
    written = set(StepDict.__annotations__) - {"icon", "feedback"}   # icon goes into metadata
    columns = set(chat_store.SCHEMA.tables["steps"].columns.keys())
    assert written <= columns, f"missing step columns: {sorted(written - columns)}"


def test_schema_has_every_thread_field_chainlit_writes():
    written = {"id", "createdAt", "name", "userId", "userIdentifier", "tags", "metadata"}
    assert written <= set(chat_store.SCHEMA.tables["threads"].columns.keys())


def test_schema_uses_only_portable_types():
    """No UUID, JSONB or ARRAY: those tie the schema to Postgres."""
    import sqlalchemy as sa
    portable = (sa.Text, sa.String, sa.Integer, sa.Boolean)
    for table in chat_store.SCHEMA.tables.values():
        for col in table.columns:
            assert isinstance(col.type, portable), f"{table.name}.{col.name}: {col.type!r}"


# ── round trip through the real data layer ──────────────────────────────────

def _steps(thread_id):
    q, t, a = (str(uuid.uuid4()) for _ in range(3))
    df = pd.DataFrame({"letter": list("MSA"), "count": [63, 55, 43]})
    meta = chat_store.answer_metadata("how many?", "M: 63", df, "qid-1",
                                      {"type": "bar", "x": "letter", "y": "count"}, ["a notice"])
    base = {"threadId": thread_id, "streaming": False, "isError": False,
            "metadata": {}, "tags": None, "language": None}
    return [
        # a user message carries the model picker selection as a dict
        {**base, "id": q, "name": "user", "type": "user_message", "output": "how many?",
         "waitForAnswer": False, "createdAt": "2026-09-26T00:00:01Z",
         "start": "2026-09-26T00:00:01Z", "end": "2026-09-26T00:00:01Z",
         "modes": {"model": "gpt4o-mini"}, "command": None},
        # a tool step writes autoCollapse
        {**base, "id": t, "parentId": q, "name": "sql_db_query", "type": "tool",
         "input": "{}", "output": "ok", "createdAt": "2026-09-26T00:00:02Z",
         "start": "2026-09-26T00:00:02Z", "end": "2026-09-26T00:00:03Z",
         "defaultOpen": False, "autoCollapse": False, "showInput": "json", "generation": None},
        {**base, "id": a, "parentId": q, "name": "Assistant", "type": "assistant_message",
         "output": "M: 63", "waitForAnswer": False, "metadata": meta,
         "createdAt": "2026-09-26T00:00:04Z", "start": "2026-09-26T00:00:04Z",
         "end": "2026-09-26T00:00:04Z", "modes": None, "command": None},
    ]


def _round_trip(tmp_path, make_layer):
    async def go():
        init_http_context()     # the data layer's write decorator needs a context
        url = f"sqlite+aiosqlite:///{(tmp_path / 'h.db').as_posix()}"
        await chat_store.ensure_schema(url)
        await chat_store.ensure_schema(url)          # idempotent
        layer = make_layer(url)
        try:
            user = await layer.create_user(User(identifier="a@tunedglobal.com", metadata={}))
            thread_id = str(uuid.uuid4())
            await layer.update_thread(thread_id, name="t", user_id=user.id,
                                      metadata={"chat_profile": "General"}, tags=["General"])
            for step in _steps(thread_id):
                await layer.create_step(step)
            return await layer.get_thread(thread_id)
        finally:
            await layer.engine.dispose()
    return asyncio.run(go())


def test_portable_layer_saves_and_reads_back_a_whole_conversation(tmp_path):
    thread = _round_trip(tmp_path, build_data_layer)
    assert [s["type"] for s in thread["steps"]] == ["user_message", "tool", "assistant_message"]
    assert json.loads(thread["metadata"])["chat_profile"] == "General"

    answer = thread["steps"][-1]
    record = chat_store.read_answer_metadata(answer["metadata"])
    assert record["question"] == "how many?" and record["answer"] == "M: 63"
    assert record["notices"] == ["a notice"]
    assert record["result"]["query_id"] == "qid-1"
    df = chat_store.preview_dataframe(record["result"])
    assert df.to_dict("list") == {"letter": ["M", "S", "A"], "count": [63, 55, 43]}


def test_stock_layer_still_loses_data_on_sqlite(tmp_path):
    """Why chainlit_data exists. If this starts failing, Chainlit has fixed
    the bugs upstream and the workarounds there can be removed."""
    thread = _round_trip(tmp_path, lambda url: SQLAlchemyDataLayer(conninfo=url))
    types = [s["type"] for s in thread["steps"]]
    assert "user_message" not in types, "stock layer now saves modes - drop the workaround"


# ── the saved result snapshot ───────────────────────────────────────────────

def test_snapshot_is_json_safe_for_awkward_types():
    df = pd.DataFrame({
        "n": np.array([1, 2], dtype=np.int64),
        "x": [1.5, np.nan],
        "day": pd.to_datetime(["2026-09-01", "2026-09-02"]),
        "name": ["a", None],
    })
    meta = chat_store.answer_metadata("q", "a", df, "qid", None, [])
    back = json.loads(json.dumps(meta))            # must survive the database
    rebuilt = chat_store.preview_dataframe(chat_store.read_answer_metadata(back)["result"])
    assert list(rebuilt.columns) == ["n", "x", "day", "name"]
    assert rebuilt["n"].tolist() == [1, 2]
    assert rebuilt["day"].iloc[0].startswith("2026-09-01")


def test_snapshot_keeps_only_the_preview_rows():
    df = pd.DataFrame({"i": range(chat_store.PREVIEW_ROWS + 50)})
    result = chat_store.answer_metadata("q", "a", df, None, None, [])["datalake"]["result"]
    assert len(result["preview"]["data"]) == chat_store.PREVIEW_ROWS
    assert result["rows"] == chat_store.PREVIEW_ROWS + 50


def test_answer_without_results_saves_no_snapshot():
    record = chat_store.answer_metadata("q", "a", None, None, None, [])["datalake"]
    assert record["result"] is None
    assert chat_store.preview_dataframe(record["result"]) is None


@pytest.mark.parametrize("metadata", [None, "", "not json", {}, {"other": 1}, "[]"])
def test_foreign_metadata_is_ignored(metadata):
    assert chat_store.read_answer_metadata(metadata) is None


def test_chainlit_still_has_the_private_hooks_resume_relies_on():
    """chainlit_app._attach_to_resumed_thread prepares elements with the
    private Element._create, and relies on on_chat_resume running before the
    thread is emitted. Re-check both after any Chainlit upgrade."""
    import inspect
    import chainlit.socket as socket
    from chainlit.element import Element
    assert "persist" in inspect.signature(Element._create).parameters
    src = inspect.getsource(socket.connection_successful)
    assert src.index("on_chat_resume(thread)") < src.index("emitter.resume_thread(thread)")


# ── saved chats and the retention sweep ─────────────────────────────────────

def _retention_db(tmp_path):
    """Four chats, 'now' being 2026-09-30:
    old      - last message 90 days ago, unsaved      -> swept
    kept     - last message 90 days ago, saved        -> kept
    active   - created 90 days ago, message yesterday -> kept (still in use)
    new      - created yesterday                      -> kept"""
    import sqlalchemy as sa
    url = f"sqlite+aiosqlite:///{(tmp_path / 'r.db').as_posix()}"
    old, recent = "2026-07-02T00:00:00.000000Z", "2026-09-29T00:00:00.000000Z"
    t, s, f = (chat_store.SCHEMA.tables[n] for n in ("threads", "steps", "feedbacks"))

    async def build():
        await chat_store.ensure_schema(url)
        async with chat_store._engine(url).begin() as conn:
            for tid, created in (("old", old), ("kept", old), ("active", old), ("new", recent)):
                await conn.execute(t.insert().values(id=tid, createdAt=created, name=tid,
                                                     userIdentifier="a@tunedglobal.com"))
            for tid, when in (("old", old), ("kept", old), ("active", recent), ("new", recent)):
                await conn.execute(s.insert().values(id=f"s-{tid}", name="user", type="user_message",
                                                     threadId=tid, streaming=False, createdAt=when))
            await conn.execute(f.insert().values(id="f-old", forId="s-old", threadId="old", value=1))
        await chat_store.set_saved(url, "kept", "a@tunedglobal.com", True)
    return url, build, (t, s, f)


def test_sweep_deletes_only_idle_unsaved_chats(tmp_path):
    from datetime import datetime, timezone
    import sqlalchemy as sa
    url, build, (t, s, f) = _retention_db(tmp_path)
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)

    async def go():
        await build()
        removed = await chat_store.purge_expired(url, days=60, now=now)
        again = await chat_store.purge_expired(url, days=60, now=now)   # idempotent
        async with chat_store._engine(url).connect() as conn:
            threads = {r[0] for r in await conn.execute(sa.select(t.c.id))}
            steps = {r[0] for r in await conn.execute(sa.select(s.c.threadId))}
            feedback = list(await conn.execute(sa.select(f.c.id)))
        await chat_store._engine(url).dispose()
        return removed, again, threads, steps, feedback

    removed, again, threads, steps, feedback = asyncio.run(go())
    assert (removed, again) == (1, 0)
    assert threads == steps == {"kept", "active", "new"}
    assert feedback == []                       # children go too, not orphaned


def test_retention_off_keeps_everything(tmp_path):
    from datetime import datetime, timezone
    url, build, _ = _retention_db(tmp_path)

    async def go():
        await build()
        n = await chat_store.purge_expired(url, days=0,
                                           now=datetime(2027, 1, 1, tzinfo=timezone.utc))
        await chat_store._engine(url).dispose()
        return n
    assert asyncio.run(go()) == 0


def test_saving_and_unsaving_a_chat(tmp_path):
    url, build, _ = _retention_db(tmp_path)

    async def go():
        await build()
        a, b = "a@tunedglobal.com", "b@tunedglobal.com"
        await chat_store.set_saved(url, "old", a, True)
        await chat_store.set_saved(url, "old", a, True)          # twice is fine
        both = await chat_store.saved_thread_ids(url, a)
        others = await chat_store.saved_thread_ids(url, b)
        await chat_store.set_saved(url, "kept", a, False)
        after = await chat_store.saved_thread_ids(url, a)
        await chat_store._engine(url).dispose()
        return both, others, after

    both, others, after = asyncio.run(go())
    assert both == {"old", "kept"}
    assert others == set()
    assert after == {"old"}


@pytest.mark.parametrize("value,days", [(None, 60), ("90", 90), ("0", 0), ("-5", 0), ("x", 60)])
def test_retention_days_setting(monkeypatch, value, days):
    if value is None:
        monkeypatch.delenv("CHAT_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("CHAT_RETENTION_DAYS", value)
    assert chat_store.retention_days() == days
