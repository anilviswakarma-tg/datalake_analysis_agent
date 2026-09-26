"""Mutable per-run state handed from the agent tools to the UI.

Tools run inside the agent loop with no access to the Streamlit call
stack, so they deposit results here and the UI collects them after."""

from __future__ import annotations

import re
import time
from typing import Any, Dict

import pandas as pd

from results import _auto_chart_spec



# ═══════════════════════════════════════════════════════════════════════════
# 3. RUN STATE (UI handoff)
# ═══════════════════════════════════════════════════════════════════════════

_LAST_RESULT: Dict[str, Any] = {
    "dataframe": None,
    "query_id": None,
    "trace": [],
    "chart": None,
    "notices": [],
}


def _reset_run_state() -> None:
    _LAST_RESULT["dataframe"] = None
    _LAST_RESULT["query_id"] = None
    _LAST_RESULT["trace"] = []
    _LAST_RESULT["chart"] = None
    _LAST_RESULT["notices"] = []
    _LAST_RESULT["executed_sql"] = []


def _record(tool_name: str, summary: str) -> None:
    _LAST_RESULT["trace"].append({
        "tool": tool_name,
        "summary": summary,
        "ts": time.time(),
    })


def _stash_result(df: pd.DataFrame) -> None:
    _LAST_RESULT["dataframe"] = df
    _LAST_RESULT["query_id"] = df.attrs.get("query_id")
    auto_chart = _auto_chart_spec(df)
    if auto_chart and _LAST_RESULT.get("chart") is None:
        _LAST_RESULT["chart"] = auto_chart


def _add_notice(text: str) -> None:
    if text not in _LAST_RESULT["notices"]:
        _LAST_RESULT["notices"].append(text)


# ── Loop guards ──────────────────────────────────────────────────────────────
# An agent with no budget will happily re-run near-identical queries forever.
# Observed 2026-09-23: a label-name lookup cycled through ten queries in 15
# minutes, alternating between precise patterns and wildcard soup, and never
# terminated. Each iteration is a paid Athena scan, so this is a cost control
# as much as a correctness one.

MAX_QUERIES_PER_RUN = 8

# A LIKE pattern with a bare "%" around a single character - '%q%a%' - is not a
# "looser" search, it is noise: it matches any string with a q followed later
# by an a ("Bquate Music Inc", "Frequency Music"). Agents reach for it when a
# literal search returns nothing, then try to exclude the noise by hand, and
# loop. Catches '%q%a%', '%q%&%a%', '%q% %&% %a%'; allows '%q&a%', '%sony%'.
_WILDCARD_SOUP = re.compile(r"%[^%']%")
_LIKE_LITERAL = re.compile(r"like\s+'([^']*)'", re.IGNORECASE)


def _normalise_sql(sql: str) -> str:
    """Whitespace- and case-insensitive form, for comparing two submissions."""
    return " ".join((sql or "").split()).rstrip(";").strip().lower()


def soup_patterns(sql: str):
    """LIKE patterns in this SQL that are wildcard soup (usually empty)."""
    return [p for p in _LIKE_LITERAL.findall(sql or "")
            if _WILDCARD_SOUP.search(p)]


def check_query_allowed(sql: str):
    """None if the query may run, otherwise the message to hand back instead.

    Returned text is addressed to the agent and is deliberately directive: a
    vague refusal just provokes a cosmetic rewrite and another loop iteration.
    """
    soup = soup_patterns(sql)
    if soup:
        return (
            "QUERY REJECTED - unusable LIKE pattern: "
            + ", ".join(repr(p) for p in soup)
            + ".\nA '%' between single characters does not loosen a search, it "
            "matches unrelated names (e.g. '%q%a%' matches 'Bquate Music Inc').\n"
            "Search the literal term instead. If that returns nothing, try real "
            "spelling variants as SEPARATE literal patterns - for 'Q&A': "
            "'%q&a%', '%q & a%', '%q and a%', '%qanda%'. If none match, report "
            "NOT FOUND and ask the user to confirm the spelling. Do NOT widen "
            "the pattern further and do NOT exclude bad matches with NOT IN."
        )

    executed = _LAST_RESULT.setdefault("executed_sql", [])
    if _normalise_sql(sql) in executed:
        return (
            "DUPLICATE QUERY - you already ran this exact SQL in this turn and "
            "it gave the result you already have. Running it again cannot "
            "change the answer.\nStop querying. Report what you found to the "
            "user, say plainly what you could not determine, and ask for the "
            "exact spelling if a name lookup came back empty."
        )

    if len(executed) >= MAX_QUERIES_PER_RUN:
        return (
            f"QUERY BUDGET EXHAUSTED - {MAX_QUERIES_PER_RUN} queries already run "
            "for this question. Stop querying now.\nSummarise what you found, "
            "state what remains unanswered, and suggest a narrower question. "
            "Do not attempt further variations."
        )
    return None


def record_query(sql: str) -> None:
    """Book a query against this run's budget, before it executes so that a
    failing query still counts and cannot be retried indefinitely."""
    _LAST_RESULT.setdefault("executed_sql", []).append(_normalise_sql(sql))


def queries_run() -> int:
    return len(_LAST_RESULT.get("executed_sql", []))
