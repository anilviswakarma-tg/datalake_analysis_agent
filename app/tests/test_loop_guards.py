"""Guards against the agent looping on a query.

Regression test for a real incident (2026-09-23): asked which owner delivers
the "Q&A" catalogue, the agent ran ten queries in fifteen minutes, alternating
between precise patterns and wildcard soup, and never terminated. Every
iteration is a billed Athena scan, so these guards are a cost control too.

The prompt already said "max 3 attempts per question". The model ignored it.
That is why these are enforced in code.
"""
import pytest

import run_state


@pytest.fixture(autouse=True)
def fresh_run():
    run_state.start_run()
    yield


# The exact patterns the agent cycled through during the incident.
SOUP = [
    "SELECT 1 WHERE LOWER(owner_name) LIKE '%q%a%'",
    "SELECT 1 WHERE LOWER(owner_name) LIKE '%q%&%a%'",
    "SELECT 1 WHERE LOWER(sub_label_name) LIKE '%q% %&% %a%'",
    "SELECT 1 WHERE LOWER(owner_name) LIKE '%q% %a%'",
]

# Legitimate searches that must keep working.
FINE = [
    "SELECT 1 WHERE LOWER(owner_name) LIKE '%q&a%'",
    "SELECT 1 WHERE LOWER(owner_name) LIKE '%q & a%'",
    "SELECT 1 WHERE LOWER(owner_name) LIKE '%q and a%'",
    "SELECT 1 WHERE LOWER(sub_label_name) LIKE '%koala%'",
    "SELECT 1 WHERE LOWER(owner_name) LIKE '%sony music entertainment%'",
    "SELECT 1 WHERE LOWER(title) LIKE '%love story%'",
    "SELECT COUNT(*) FROM t WHERE status = 1",
]


@pytest.mark.parametrize("sql", SOUP)
def test_wildcard_soup_is_rejected(sql):
    msg = run_state.check_query_allowed(sql)
    assert msg is not None, f"should have been rejected: {sql}"
    assert "unusable LIKE pattern" in msg
    # The refusal must tell the agent what to do instead, or it just rewrites.
    assert "%q&a%" in msg and "NOT FOUND" in msg


@pytest.mark.parametrize("sql", FINE)
def test_legitimate_searches_are_allowed(sql):
    assert run_state.check_query_allowed(sql) is None, f"false positive: {sql}"


def test_exact_repeat_is_blocked():
    sql = "SELECT owner_name FROM mastermusic WHERE status = 1 LIMIT 10"
    assert run_state.check_query_allowed(sql) is None
    run_state.record_query(sql)
    msg = run_state.check_query_allowed(sql)
    assert msg is not None and "DUPLICATE QUERY" in msg


def test_repeat_detection_ignores_whitespace_and_case():
    run_state.record_query("SELECT a FROM t WHERE x = 1")
    msg = run_state.check_query_allowed("select   a\n  from t\n  where x = 1;")
    assert msg is not None and "DUPLICATE QUERY" in msg


def test_budget_is_enforced():
    for i in range(run_state.MAX_QUERIES_PER_RUN):
        sql = f"SELECT {i} FROM t"
        assert run_state.check_query_allowed(sql) is None
        run_state.record_query(sql)
    msg = run_state.check_query_allowed("SELECT 999 FROM t")
    assert msg is not None and "BUDGET EXHAUSTED" in msg


def test_budget_counts_failed_queries_too():
    """A query that errors must still consume budget, or a failing query can be
    retried forever."""
    for i in range(run_state.MAX_QUERIES_PER_RUN):
        run_state.record_query(f"SELECT {i} FROM broken_table")
    assert "BUDGET EXHAUSTED" in run_state.check_query_allowed("SELECT x FROM t")


def test_budget_resets_between_questions():
    for i in range(run_state.MAX_QUERIES_PER_RUN):
        run_state.record_query(f"SELECT {i} FROM t")
    assert run_state.check_query_allowed("SELECT new FROM t") is not None
    run_state.start_run()
    assert run_state.check_query_allowed("SELECT new FROM t") is None
    assert run_state.queries_run() == 0


def test_the_actual_incident_terminates():
    """Replay the real sequence; it must be stopped, not run to completion."""
    incident = [
        "SELECT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q%a%'",
        "SELECT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q&a%' OR LOWER(owner_name) LIKE '%q & a%'",
        "SELECT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q%a%'",
        "SELECT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q&a%' OR LOWER(owner_name) LIKE '%q and a%'",
        "SELECT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q%a%'",
        "SELECT DISTINCT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q%a%'",
        "SELECT DISTINCT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q%&%a%'",
        "SELECT DISTINCT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q%a%' AND owner_name NOT IN ('Bquate Music Inc')",
        "SELECT DISTINCT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q% %a%'",
        "SELECT DISTINCT owner_name FROM mm WHERE LOWER(owner_name) LIKE '%q%&%a%'",
    ]
    executed, blocked = 0, 0
    for sql in incident:
        if run_state.check_query_allowed(sql) is None:
            run_state.record_query(sql)
            executed += 1
        else:
            blocked += 1
    # 8 of the 10 were wildcard soup and are refused outright; the rest are
    # within budget. The point is that most of the scans never happen.
    assert blocked >= 7, f"only {blocked} of 10 blocked"
    assert executed <= 3, f"{executed} queries still ran"
