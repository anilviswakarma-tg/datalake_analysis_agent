"""Guards from the 2026-10-05 incident: the deployed agent ran one 22 GiB
query 64 times (and another 19), each time as a fresh question, so the
per-question guards never saw a repeat.

These replay that shape against a fake Athena and check that repeats are
served by Athena's result reuse (scanning nothing, with a notice to the
user), that a large result is described rather than re-run, and that the
chat's scan budget stops the agent."""
import inspect

import pytest

import aws
import run_state
import tools

GB = 1024 ** 3
LOOPED_SQL = ("WITH track_rights AS ( SELECT m.isrc FROM \"tg-deltalake-bronze\".\"track_active\" ta "
              "JOIN \"tg-deltalake-bronze\".\"mastermusic\" m ON ta.track_id = m.id "
              "WHERE ta.group_id = 'SPLH' ) SELECT * FROM track_rights")


class Athena:
    """Counts what the app asks of Athena; returns `rows` result rows."""

    def __init__(self, rows=5, scanned=22 * GB, reused=False, reuse_repeats=False):
        self.rows, self.scanned, self.reused = rows, scanned, reused
        self.reuse_repeats = reuse_repeats      # behave like Athena result reuse
        self.started, self.read, self._seen = [], [], set()

    def start_query_execution(self, **kw):
        self.started.append(kw)
        qid = f"q{len(self.started)}"
        if self.reuse_repeats:
            self._reused_ids = getattr(self, "_reused_ids", set())
            if kw["QueryString"] in self._seen:
                self._reused_ids.add(qid)
            self._seen.add(kw["QueryString"])
        return {"QueryExecutionId": qid}

    def get_query_execution(self, QueryExecutionId):
        reused = self.reused or QueryExecutionId in getattr(self, "_reused_ids", set())
        return {"QueryExecution": {
            "Status": {"State": "SUCCEEDED"},
            "Statistics": {"DataScannedInBytes": 0 if reused else self.scanned,
                           "ResultReuseInformation": {"ReusedPreviousResult": reused}},
            "ResultConfiguration": {"OutputLocation": f"s3://b/{QueryExecutionId}.csv"}}}

    def get_paginator(self, name):
        fake = self

        class Pages:
            def paginate(self, QueryExecutionId):
                fake.read.append(QueryExecutionId)
                body = [{"Data": [{"VarCharValue": str(i)}]} for i in range(fake.rows)]
                yield {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Label": "isrc"}]},
                                     "Rows": [{"Data": [{"VarCharValue": "isrc"}]}] + body}}
        return Pages()


@pytest.fixture
def athena(monkeypatch):
    def install(**kw):
        fake = Athena(**kw)
        monkeypatch.setattr(aws, "_athena", lambda: fake)
        monkeypatch.setattr(aws, "result_size_bytes", lambda qid: 3_100_000)
        monkeypatch.setattr(tools, "result_size_bytes", lambda qid: 3_100_000)
        return fake
    return install


def _ask(ledger, sql=LOOPED_SQL):
    """One question in the chat that owns `ledger`, running `sql`."""
    ctx = run_state.start_run(question="SPLH rights", session=ledger)
    ctx.dictionary_loaded = True
    return tools.sql_db_query.func(sql), ctx


def test_athena_result_reuse_is_on_for_every_query(athena):
    fake = athena()
    _ask(run_state.SessionLedger())
    reuse = fake.started[0]["ResultReuseConfiguration"]["ResultReuseByAgeConfiguration"]
    assert reuse == {"Enabled": True, "MaxAgeInMinutes": 120}


def test_the_incident_replayed_scans_once(athena):
    """64 fresh questions, same SQL: Athena's result reuse serves 63 of them
    from the stored result, so the chat is billed for one scan, not 64."""
    fake = athena(reuse_repeats=True)
    ledger = run_state.SessionLedger()
    replies = [_ask(ledger) for _ in range(64)]
    assert len(fake.started) == 64                       # still submitted...
    assert ledger.bytes_scanned == 22 * GB               # ...but scanned once
    assert "Reused a stored result: nothing scanned." not in replies[0][0]
    assert all("Reused a stored result: nothing scanned." in r for r, _ in replies[1:])


def test_within_one_question_a_repeat_is_still_refused(athena):
    """The per-question guard stays: re-running inside one question means
    the agent is looping, even when Athena would serve it for free."""
    fake = athena()
    ctx = run_state.start_run(session=run_state.SessionLedger())
    ctx.dictionary_loaded = True
    tools.sql_db_query.func(LOOPED_SQL)
    assert tools.sql_db_query.func(LOOPED_SQL).startswith("DUPLICATE QUERY")
    assert len(fake.started) == 1


def test_a_large_result_is_described_not_rerun(athena):
    """The brief's other suspect: a big result read as 'no answer yet'."""
    athena(rows=30_000)
    reply, ctx = _ask(run_state.SessionLedger())
    assert "More than 100 rows: only the first 100 were loaded" in reply
    assert "the full result is 3.0 MB" in reply
    assert "Never re-run the same SQL" in reply
    assert len(ctx.dataframe) == 100                       # never loads it all


def test_a_small_result_says_it_is_complete(athena):
    athena(rows=15)
    reply, _ = _ask(run_state.SessionLedger())
    assert "15 rows returned (all of them)" in reply


def test_the_chat_scan_budget_stops_new_queries(athena, monkeypatch):
    monkeypatch.setenv("ATHENA_SESSION_SCAN_BUDGET_GB", "50")
    fake = athena(scanned=22 * GB)
    ledger = run_state.SessionLedger()
    _ask(ledger, "SELECT 1"), _ask(ledger, "SELECT 2"), _ask(ledger, "SELECT 3")   # 66 GiB
    reply, _ = _ask(ledger, "SELECT 4")
    assert reply.startswith("SCAN BUDGET REACHED") and len(fake.started) == 3
    ledger.extend()                                        # the user chose Continue
    reply, _ = _ask(ledger, "SELECT 4")
    assert reply.startswith("Query succeeded") and len(fake.started) == 4


def test_athena_reported_reuse_counts_as_free(athena):
    athena(reused=True)
    _, ctx = _ask(run_state.SessionLedger())
    assert ctx.scans[0]["status"] == "reused" and ctx.scans[0]["bytes_scanned"] == 0


def test_budget_zero_means_no_budget(monkeypatch):
    monkeypatch.setenv("ATHENA_SESSION_SCAN_BUDGET_GB", "0")
    ledger = run_state.SessionLedger()
    ledger.bytes_scanned = 10 ** 15
    assert not ledger.over_budget()


def test_both_uis_keep_the_ledger_on_the_chat_not_the_question():
    """The incident's root cause: guards that reset with every question."""
    import app
    import chainlit_app
    import ui
    assert 'st.session_state.setdefault("ledger", SessionLedger())' in inspect.getsource(app.main)
    assert 'session=ledger' in inspect.getsource(app.main)
    assert 'st.session_state.pop("ledger", None)' in inspect.getsource(ui._clear_current_runs)
    # Chainlit keeps it by thread id (tests/test_reconnect.py)
    src = inspect.getsource(chainlit_app._answer)
    assert "ledger = _ledger()" in src
    assert "session=ledger" in src and "_continue_past_budget(ledger)" in src


def test_users_are_told_when_a_result_is_reused(athena):
    """Users see a notice above the answer whenever Athena served a cached
    result."""
    athena(reused=True)
    _, ctx = _ask(run_state.SessionLedger())
    assert any("Cached result" in n and "up to 120 minutes" in n for n in ctx.notices)


def test_a_date_compared_with_text_gets_the_fix():
    msg = tools._athena_error("TYPE_MISMATCH: line 2:46: Cannot apply operator: date <= varchar(10)")
    assert "DATE '2026-09-01'" in msg
