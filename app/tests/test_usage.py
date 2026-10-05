"""Athena usage: what each query scanned, the per-query scan cutoff, and the
hand-over to the browser, which keeps the session total.

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


# ── handing the scans to the browser ────────────────────────────────────────

@pytest.fixture
def cl_app(monkeypatch):
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    import importlib
    import chainlit_app
    return importlib.reload(chainlit_app)


def test_scans_are_handed_over_once_per_user(cl_app, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    monkeypatch.setattr(cl_app, "workgroup_scan_cutoff", lambda: 50 * GB)
    scans = [{"bytes_scanned": 5, "status": "succeeded"}, {"bytes_scanned": 7, "status": "failed"}]
    cl_app._hand_over_scans("a@tunedglobal.com", scans)
    cl_app._hand_over_scans("b@tunedglobal.com", scans[:1])
    a = SimpleNamespace(identifier="a@tunedglobal.com")
    first = asyncio.run(cl_app.collect_usage(a)).body
    second = asyncio.run(cl_app.collect_usage(a)).body
    assert b'"bytes":5' in first and b'"bytes":7' in first
    assert b'"scans":[]' in second                      # collected, then forgotten
    assert b'"query_limit_bytes":53687091200' in first   # the bar's scale
    assert "b@tunedglobal.com" in cl_app._PENDING_SCANS  # other users untouched


def test_a_failed_run_still_hands_over_its_scans(cl_app):
    src = inspect.getsource(cl_app.on_message)
    assert "finally:\n        # A failed run's queries were billed too\n        _hand_over_scans(" in src
    assert "user=_user_key()" in src


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


def test_the_per_query_cutoff_is_read_from_the_workgroup(monkeypatch):
    class Athena:
        def get_work_group(self, WorkGroup):
            return {"WorkGroup": {"Configuration": {"BytesScannedCutoffPerQuery": 50 * GB}}}
    monkeypatch.setattr(aws, "_athena", lambda: Athena())
    aws._cutoff_for.cache_clear()
    assert aws._cutoff_for("datalake-agent") == 50 * GB

    class Denied:
        def get_work_group(self, WorkGroup):
            raise RuntimeError("AccessDenied")
    monkeypatch.setattr(aws, "_athena", lambda: Denied())
    aws._cutoff_for.cache_clear()
    assert aws._cutoff_for("datalake-agent") is None    # the bar is hidden then
    aws._cutoff_for.cache_clear()


def test_the_agent_uses_its_own_workgroup_by_default(monkeypatch):
    """Not `primary`, which the reporting pipelines share."""
    import config
    monkeypatch.delenv("ATHENA_WORKGROUP", raising=False)
    assert config._workgroup() == "datalake-agent"
    assert config.DEFAULTS["ATHENA_WORKGROUP"] == "datalake-agent"
