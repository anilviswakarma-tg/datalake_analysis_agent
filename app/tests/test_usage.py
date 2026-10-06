"""Athena usage: what each query scanned, the per-query scan cutoff, the
usage stored per user, and capped/uncapped tiers.

A fake Athena client stands in for AWS, so these run offline."""
import inspect

import pytest

import aws
import run_state
import tools

GB = 1024 ** 3


class FakeAthena:
    """Answers start/get/stop/paginate the way boto3's Athena client does."""

    def __init__(self, state="SUCCEEDED", scanned=3 * GB, reason="", running_forever=False):
        self.state, self.scanned, self.reason = state, scanned, reason
        self.running_forever = running_forever
        self.stopped = []

    def start_query_execution(self, **kw):
        self.workgroup = kw["WorkGroup"]
        return {"QueryExecutionId": "qid-1"}

    def get_query_execution(self, QueryExecutionId):
        state = "RUNNING" if self.running_forever else self.state
        return {"QueryExecution": {
            "Status": {"State": state, "StateChangeReason": self.reason},
            "Statistics": {"DataScannedInBytes": self.scanned}}}

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)

    def get_paginator(self, name):
        class Pages:
            def paginate(self, **kw):
                yield {"ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Label": "n"}]},
                    "Rows": [{"Data": [{"VarCharValue": "n"}]},
                             {"Data": [{"VarCharValue": "42"}]}]}}
        return Pages()


@pytest.fixture
def athena(monkeypatch):
    def install(**kw):
        fake = FakeAthena(**kw)
        monkeypatch.setattr(aws, "_athena", lambda: fake)
        monkeypatch.setattr(aws.time, "sleep", lambda s: None)
        return fake
    return install


def test_a_successful_query_reports_what_it_scanned(athena, monkeypatch):
    monkeypatch.setenv("ATHENA_WORKGROUP", "datalake-agent")
    fake = athena(scanned=3 * GB)
    ctx = run_state.start_run(user="a@tunedglobal.com")
    df = run_state.tracked_query("SELECT 42", "sql_db_query")
    assert df["n"].tolist() == [42]
    assert fake.workgroup == "datalake-agent"
    assert ctx.scans == [{"query_id": "qid-1", "bytes_scanned": 3 * GB, "status": "succeeded",
                          "tool": "sql_db_query", "workgroup": "datalake-agent"}]
    assert ctx.bytes_scanned == 3 * GB


def test_a_failed_query_is_still_counted(athena):
    """Athena bills what a failed or cancelled query scanned."""
    athena(state="CANCELLED", scanned=10 * GB,
           reason="Query cancelled! : Bytes scanned limit was exceeded")
    ctx = run_state.start_run()
    with pytest.raises(aws.AthenaQueryError) as e:
        run_state.tracked_query("SELECT * FROM big", "sql_db_query")
    assert e.value.scan_cutoff and e.value.bytes_scanned == 10 * GB
    assert ctx.scans[0]["status"] == "cancelled" and ctx.bytes_scanned == 10 * GB


def test_queries_outside_a_question_are_not_recorded(athena):
    """e.g. the entity cache warming up: there is no one to bill it to."""
    import contextvars
    athena()
    # A fresh context has no run: the query must still work, unrecorded.
    df = contextvars.Context().run(run_state.tracked_query, "SELECT 1", "entity lookup")
    assert df["n"].tolist() == [42]


def test_a_timed_out_query_is_stopped_not_abandoned(athena, monkeypatch):
    """Giving up waiting used to leave the query scanning, and billing."""
    fake = athena(running_forever=True)
    monkeypatch.setattr(aws, "QUERY_TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError, match="was stopped"):
        aws._run_athena_query("SELECT * FROM big")
    assert fake.stopped == ["qid-1"]


def test_the_agent_is_told_how_to_recover_from_the_scan_cutoff(athena):
    athena(state="CANCELLED", scanned=10 * GB,
           reason="Query cancelled! : Bytes scanned limit was exceeded")
    ctx = run_state.start_run()
    ctx.dictionary_loaded = True
    msg = tools.sql_db_query.func("SELECT * FROM big")
    assert msg.startswith("QUERY STOPPED") and "10.0 GB" in msg
    assert "partition filters" in msg and "Don't retry it as is" in msg


def test_other_failures_keep_the_plain_athena_error(athena):
    athena(state="FAILED", scanned=0, reason="COLUMN_NOT_FOUND: Column 'x' cannot be resolved")
    ctx = run_state.start_run()
    ctx.dictionary_loaded = True
    msg = tools.sql_db_query.func("SELECT x FROM t")
    assert msg.startswith("ATHENA ERROR") and "describe_table" in msg


def test_every_athena_call_goes_through_the_tracker():
    """A direct _run_athena_query call would be missing from the totals."""
    import entities
    for module in (tools, entities):
        assert "_run_athena_query(" not in inspect.getsource(module), module.__name__


# ── usage stored per user, and tiers ────────────────────────────────────────

import asyncio
from datetime import datetime, timezone

import chat_store


def _scan(n, status="succeeded"):
    return {"query_id": f"q{n}", "bytes_scanned": n, "status": status,
            "tool": "sql_db_query", "workgroup": "datalake-agent"}


def test_usage_is_totalled_for_the_calendar_month(db_url):
    url = db_url
    a = "a@tunedglobal.com"

    async def go():
        await chat_store.ensure_schema(url)
        sept = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
        octo = datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)
        await chat_store.record_usage(url, a, "t1", [_scan(100)], now=sept)
        await chat_store.record_usage(url, a, "t1", [_scan(5 * GB), _scan(7, "failed")], now=octo)
        await chat_store.record_usage(url, "b@tunedglobal.com", None, [_scan(999)], now=octo)
        oct_usage = await chat_store.month_usage(url, a, now=datetime(2026, 10, 5, tzinfo=timezone.utc))
        sep_usage = await chat_store.month_usage(url, a, now=sept)
        nobody = await chat_store.month_usage(url, "c@tunedglobal.com")
        await chat_store._engine(url).dispose()
        return oct_usage, sep_usage, nobody

    oct_usage, sep_usage, nobody = asyncio.run(go())
    assert oct_usage == {"bytes": 5 * GB + 7, "queries": 2, "since": "2026-10-01T00:00:00Z"}
    assert sep_usage["bytes"] == 100 and sep_usage["queries"] == 1
    assert nobody["bytes"] == 0 and nobody["queries"] == 0


def test_users_are_capped_unless_marked_uncapped(db_url):
    url = db_url

    async def go():
        await chat_store.ensure_schema(url)
        before = await chat_store.user_tier(url, "a@tunedglobal.com")
        await chat_store.set_user_tier(url, "a@tunedglobal.com", chat_store.UNCAPPED)
        after = await chat_store.user_tier(url, "a@tunedglobal.com")
        await chat_store.set_user_tier(url, "a@tunedglobal.com", chat_store.CAPPED)
        back = await chat_store.user_tier(url, "a@tunedglobal.com")
        with pytest.raises(ValueError):
            await chat_store.set_user_tier(url, "a@tunedglobal.com", "gold")
        await chat_store._engine(url).dispose()
        return before, after, back

    assert asyncio.run(go()) == ("capped", "uncapped", "capped")


def test_usage_outlives_the_chats_it_came_from():
    """No foreign key to threads: the retention sweep deletes chats, not usage."""
    usage = chat_store.SCHEMA.tables["query_usage"]
    assert not usage.foreign_keys
    assert "query_usage" not in "".join(
        __import__("inspect").getsource(chat_store._delete_threads))


@pytest.fixture
def cl_app(monkeypatch):
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    import importlib
    import chainlit_app
    return importlib.reload(chainlit_app)


def test_every_question_saves_its_scans_even_when_it_fails(cl_app):
    src = inspect.getsource(cl_app._answer)
    finally_block = src.split("finally:")[1].split("\n\n")[0]
    assert "await _save_usage(ctx.user, ctx.scans)" in finally_block
    assert "user=_user_key()" in src


def test_a_storage_failure_never_fails_the_answer(cl_app, monkeypatch, caplog):
    async def broken(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(cl_app.chat_store, "record_usage", broken)
    monkeypatch.setattr(cl_app, "STORE", cl_app.storage.SqlStore("sqlite+aiosqlite:///x.db"))
    from types import SimpleNamespace
    monkeypatch.setattr(cl_app.cl, "context", SimpleNamespace(session=SimpleNamespace(thread_id="t")))
    asyncio.run(cl_app._save_usage("a@tunedglobal.com", [_scan(1)]))     # no exception
    assert "could not record Athena usage" in caplog.text


def test_the_meter_gets_the_month_and_the_tier(cl_app, monkeypatch, db_url):
    from types import SimpleNamespace
    url = db_url
    monkeypatch.setattr(cl_app, "STORE", cl_app.storage.SqlStore(url))

    async def go():
        await chat_store.ensure_schema(url)
        await chat_store.record_usage(url, "a@tunedglobal.com", None, [_scan(42)])
        response = await cl_app.usage_summary(SimpleNamespace(identifier="a@tunedglobal.com"))
        await chat_store._engine(url).dispose()
        return response.body

    body = asyncio.run(go())
    assert b'"bytes":42' in body and b'"queries":1' in body and b'"tier":"capped"' in body


def test_usage_route_is_post_only(cl_app):
    import chainlit.server as server
    methods = {tuple(sorted(r.methods)) for r in server.app.routes
               if getattr(r, "path", "").endswith("/datalake/usage")}
    assert methods == {("POST",)}


def test_downloads_follow_the_workgroups_own_output_location(monkeypatch):
    """A workgroup that enforces its own results bucket writes there, not to
    ATHENA_OUTPUT_S3; a guessed path would quietly cut every download down
    to the 100-row preview."""
    class Athena:
        def get_query_execution(self, QueryExecutionId):
            return {"QueryExecution": {"ResultConfiguration": {
                "OutputLocation": f"s3://agent-results/athena/{QueryExecutionId}.csv"}}}
    monkeypatch.setenv("ATHENA_OUTPUT_S3", "s3://somewhere-else/")
    monkeypatch.setattr(aws, "_athena", lambda: Athena())
    assert aws._result_location("q1") == ("agent-results", "athena/q1.csv")


def test_the_agent_uses_its_own_workgroup_by_default(monkeypatch):
    """Not `primary`, which the reporting pipelines share."""
    import config
    monkeypatch.delenv("ATHENA_WORKGROUP", raising=False)
    assert config._workgroup() == "datalake-agent"
    assert config.DEFAULTS["ATHENA_WORKGROUP"] == "datalake-agent"


def test_the_agent_is_told_when_there_are_no_downloads(athena, monkeypatch):
    """A single value gets no table or downloads; the agent used to promise
    them anyway ("download the detailed results below")."""
    import pandas as pd
    athena()                                   # returns one row, one column
    ctx = run_state.start_run()
    ctx.dictionary_loaded = True
    single = tools.sql_db_query.func("SELECT count(*) AS n FROM t")
    assert "NO download buttons" in single and "don't mention" in single

    table = pd.DataFrame({"label": ["a", "b"], "n": [1, 2]})
    table.attrs["query_id"] = "q2"
    monkeypatch.setattr(tools, "tracked_query", lambda *a, **k: table)
    run_state.start_run().dictionary_loaded = True
    listed = tools.sql_db_query.func("SELECT label, n FROM t")
    assert "CSV and Excel download buttons" in listed
