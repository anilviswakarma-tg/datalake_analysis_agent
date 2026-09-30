"""The tools exposed to the agent: knowledge lookup, schema discovery,
entity resolution, SQL validation and execution, and UI affordances."""

from __future__ import annotations

from typing import List

import pandas as pd
from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.tools import tool

import models
from aws import _glue, _run_athena_query, _with_credential_retry
from config import DATA_DICT_DIR, VALID_DOMAINS, _known_databases
from entities import _fuzzy_match, _load_groups, _load_musicowners
from knowledge import (_append_feedback, _data_dict_index, _data_dict_preamble,
                       _normalise_table_name)
from results import CHART_TYPES
from run_state import (_add_notice, _record, _stash_result, check_query_allowed,
                       current_run, record_query)



# ═══════════════════════════════════════════════════════════════════════════
# 6. AGENT TOOLS
# ═══════════════════════════════════════════════════════════════════════════

# ---- Knowledge tools ----

@tool
def get_data_dictionary(tables: str) -> str:
    """Look up the AUTHORITATIVE reference for one or more tables. Call this
    FIRST, before writing any SQL, for every
    table the query will touch.

    This is the data team's maintained data dictionary: what each table and
    column actually MEANS, which columns are nullable or mis-typed, how tables
    join, refresh cadence, and the caveats that make a plausible-looking query
    wrong. When it disagrees with anything else, IT WINS.

    Args:
      tables: one or more table names, comma-separated. Database prefixes are
        fine and ignored, e.g. "mastermusic, track_active" or
        '"tg-deltalake-bronze"."mastermusic"'.

    Returns the shared cross-table rules plus the full reference for each
    table. Unknown names come back with the list of documented tables.
    """
    index = _data_dict_index()
    # Consulted, whatever comes back: the gate in sql_db_query is about the
    # agent having looked, and an unknown name returns the documented list.
    current_run().dictionary_loaded = True
    if not index:
        _record("get_data_dictionary", "⚠️ dictionary missing or unreadable")
        return ("The data dictionary at knowledge/data-dictionary/ is missing or "
                "unreadable. Fall back to "
                "discovery via list_tables/describe_table.")

    requested = [t for t in (x.strip() for x in tables.split(",")) if t]
    if not requested:
        return f"Name at least one table. Documented tables: {sorted(set(index))}"

    parts: List[str] = []
    resolved: List[str] = []
    seen_docs: set = set()
    unknown: List[str] = []

    for raw in requested:
        name = _normalise_table_name(raw)
        doc = index.get(name)
        if doc is None:
            unknown.append(raw)
            continue
        if doc in seen_docs:          # e.g. playlists + radiostations share a doc
            resolved.append(name)
            continue
        try:
            parts.append((DATA_DICT_DIR / doc).read_text(encoding="utf-8").strip())
            seen_docs.add(doc)
            resolved.append(name)
        except OSError as e:
            unknown.append(f"{raw} (doc {doc} unreadable: {e})")

    if not parts:
        _record("get_data_dictionary", f"⚠️ no docs for {requested}")
        return (f"No data dictionary entry for {unknown}. Documented tables: "
                f"{sorted(set(index))}. For an undocumented table, use "
                f"describe_table(db, table) and proceed carefully.")

    out = [p for p in (_data_dict_preamble(),) if p] + parts
    if unknown:
        out.append(
            f"/* NOT DOCUMENTED in the data dictionary: {unknown}. "
            f"Use describe_table(db, table) for those, proceed carefully, and "
            f"consider capture_finding(...) if you learn something worth keeping. */"
        )
    _record("get_data_dictionary", f"📗 {', '.join(resolved) or '—'}"
                                   + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
    return "\n\n---\n\n".join(out)


@tool
def capture_finding(domain: str, observation: str) -> str:
    """Record an observation in knowledge/feedback.md for later human review.

    Call this when you discover something useful that ISN'T already in the
    domain knowledge — e.g. a column behaves unexpectedly, a table has an
    unusual filter requirement, a join pattern that's not documented yet.

    Args:
      domain: 'catalogue', 'playlog', 'store', or 'other'
      observation: 1-3 sentences describing what you learned. Be specific.

    Don't call this for routine successes. Only call when there's actionable
    new information for the team to consider adding to the data dictionary.
    """
    d = domain.strip().lower()
    if d not in VALID_DOMAINS and d != "other":
        d = "other"
    try:
        _append_feedback(d, observation, current_run().question or None)
        _record("capture_finding", f"📝 recorded ({d}): {observation[:60]}...")
        return "Recorded. A reviewer will see this in feedback.md."
    except Exception as e:
        _record("capture_finding", f"❌ {e}")
        return f"Could not record finding: {e}"


# ---- Discovery tools ----

@tool
def list_databases() -> str:
    """List the databases known to this agent (bronze, silver, master).
    Use this to confirm which databases are available before describing
    tables in them."""
    dbs = _known_databases()
    _record("list_databases", f"{len(dbs)} databases")
    return "Known databases:\n" + "\n".join(f"  - {d}" for d in dbs)


@tool
def list_tables(database: str = "") -> str:
    """List tables in a database. Useful when you need to find an
    undocumented table.

    Args:
      database: e.g. 'tg-deltalake-bronze'. If empty, lists tables in all
        known databases.
    """
    targets = [database.strip()] if database.strip() else _known_databases()

    output: List[str] = []
    total = 0
    for db in targets:
        try:
            tables: List[str] = _with_credential_retry(
                lambda db=db: [
                    t["Name"]
                    for page in _glue().get_paginator("get_tables").paginate(DatabaseName=db)
                    for t in page.get("TableList", [])
                ]
            )
            output.append(f"\n{db}:")
            for t in tables:
                output.append(f"  - {t}")
            total += len(tables)
        except (BotoCoreError, ClientError) as e:
            output.append(f"\n{db}:  (error: {e})")

    _record("list_tables", f"{total} tables across {len(targets)} db(s)")
    return "\n".join(output)


@tool
def describe_table(database: str, table: str) -> str:
    """Get the schema (column names + types + partition keys) and 3 sample
    rows for a table. Use this when you encounter a table not documented in
    the data dictionary, or to confirm exact column names.

    Args:
      database: e.g. 'tg-deltalake-bronze', 'tg-deltalake-silver', 'tg-master'
      table: table name
    """
    db = database.strip().strip('"')
    tbl = table.strip().strip('"')

    out_parts: List[str] = []

    # Schema from Glue. Uses a fresh client per retry attempt in case of a
    # transient IMDS hiccup (see _with_credential_retry).
    try:
        resp = _with_credential_retry(lambda: _glue().get_table(DatabaseName=db, Name=tbl))
        t = resp["Table"]
        cols = t.get("StorageDescriptor", {}).get("Columns", [])
        parts = t.get("PartitionKeys", [])
        lines = [f'-- Table: "{db}"."{tbl}"']
        if parts:
            lines.append(f"-- Partitioned by: {', '.join(p['Name'] for p in parts)}")
        lines.append("")
        lines.append("Columns:")
        for c in cols:
            lines.append(f"  {c['Name']:<28} {c['Type']}")
        if parts:
            lines.append("")
            lines.append("Partition columns:")
            for p in parts:
                lines.append(f"  {p['Name']:<28} {p['Type']}  -- PARTITION")
        out_parts.append("\n".join(lines))
    except (BotoCoreError, ClientError) as e:
        _record("describe_table", f"❌ schema lookup failed: {e}")
        return f"Error fetching schema for {db}.{tbl}: {e}"

    # Sample 3 rows via Athena
    try:
        sample = _run_athena_query(
            f'SELECT * FROM "{db}"."{tbl}" LIMIT 3', database=db, limit_rows=3
        )
        if len(sample) > 0:
            out_parts.append(
                f"/* 3 sample rows from {tbl}:\n"
                + sample.to_string(index=False, max_cols=8)[:1500]
                + "\n*/"
            )
    except Exception as e:
        out_parts.append(f"/* (could not sample {tbl}: {str(e)[:120]}) */")

    _record("describe_table", f"📐 {db}.{tbl}")
    return "\n\n".join(out_parts)


@tool
def count_rows(database: str, table: str, where_clause: str = "") -> str:
    """Quick row count for a table (with optional WHERE). Useful as a sanity
    check before writing a complex query.

    Args:
      database: e.g. 'tg-deltalake-bronze'
      table: table name
      where_clause: optional WHERE clause WITHOUT the 'WHERE' keyword
        (e.g. "status = 1 AND dw_stock_type = 'track'")
    """
    db = database.strip().strip('"')
    tbl = table.strip().strip('"')
    where_clause = where_clause.strip()

    sql = f'SELECT COUNT(*) AS row_count FROM "{db}"."{tbl}"'
    if where_clause:
        sql += f"\nWHERE {where_clause}"

    try:
        df = _run_athena_query(sql, database=db, limit_rows=1)
        count = df.iloc[0, 0] if len(df) > 0 else 0
        _record("count_rows", f"🔢 {db}.{tbl} → {count:,}")
        return f"Row count: {count:,}"
    except (BotoCoreError, ClientError, RuntimeError, TimeoutError) as e:
        _record("count_rows", f"❌ {e}")
        return f"Error: {e}"


# ---- Entity resolution tools ----

@tool
def resolve_label(name: str) -> str:
    """Resolve a human-readable label/owner name (e.g. 'Sony', 'Universal',
    'Warner') to a numeric owner_id. ALWAYS call this before generating SQL
    that filters on a label.

    Returns:
      - "RESOLVED: owner_id=<id>, name='<name>'" on single confident match
      - "AMBIGUOUS: <list>" — relay to user, ask them to clarify
      - "NOT_FOUND: ..." — tell the user the label isn't recognized
    """
    try:
        df = _load_musicowners()
        matches = _fuzzy_match(df, name, ["name", "display_name"])
    except (BotoCoreError, ClientError, RuntimeError, TimeoutError) as e:
        msg = f"Error querying musicowners: {e}"
        _record("resolve_label", f"❌ {msg[:80]}")
        return f"ERROR: {msg}"

    if len(matches) == 0:
        _record("resolve_label", f"⚠️ no match for '{name}'")
        return (f"NOT_FOUND: No active label matches {name!r}. "
                f"Ask the user to verify the spelling.")

    top = matches.iloc[0]
    if top["score"] == 100 or len(matches) == 1:
        _record("resolve_label", f"✅ '{name}' → owner_id={top['id']} ({top['name']})")
        return f"RESOLVED: owner_id={top['id']}, name='{top['name']}'"

    if len(matches) >= 2 and top["score"] >= matches.iloc[1]["score"] + 20:
        _record("resolve_label", f"✅ '{name}' → owner_id={top['id']} ({top['name']})")
        return f"RESOLVED: owner_id={top['id']}, name='{top['name']}'"

    top_matches = matches.head(10)
    listing = "\n".join(
        f"  - owner_id={r['id']}, name={r['name']!r}, display_name={r['display_name']!r}"
        for _, r in top_matches.iterrows()
    )
    _record("resolve_label", f"⚠️ ambiguous '{name}' ({len(matches)} matches)")
    return (
        f"AMBIGUOUS: Found {len(matches)} labels matching {name!r}. "
        f"Show this list to the user and ask which one they meant:\n{listing}"
    )


@tool
def resolve_client(name: str) -> str:
    """Resolve a human-readable client name (e.g. 'GMM Thailand', 'Fluxus',
    'GMTH', 'FLUS') to a group_id PARTITION KEY value. ALWAYS call this
    before SQL that filters by client.

    Returns:
      - "RESOLVED: group_id='<id>', name='<name>'" on single confident match
      - "AMBIGUOUS: <list>" — relay to user
      - "NOT_FOUND: ..."
    """
    try:
        df = _load_groups()
        matches = _fuzzy_match(df, name, ["name", "group_id"])
    except (BotoCoreError, ClientError, RuntimeError, TimeoutError) as e:
        msg = f"Error querying groups: {e}"
        _record("resolve_client", f"❌ {msg[:80]}")
        return f"ERROR: {msg}"

    if len(matches) == 0:
        _record("resolve_client", f"⚠️ no match for '{name}'")
        return (f"NOT_FOUND: No client matches {name!r}. "
                f"Ask the user to verify the spelling.")

    top = matches.iloc[0]
    if top["score"] == 100 or len(matches) == 1:
        _record("resolve_client", f"✅ '{name}' → group_id={top['group_id']!r} ({top['name']})")
        return f"RESOLVED: group_id='{top['group_id']}', name='{top['name']}'"

    if len(matches) >= 2 and top["score"] >= matches.iloc[1]["score"] + 20:
        _record("resolve_client", f"✅ '{name}' → group_id={top['group_id']!r} ({top['name']})")
        return f"RESOLVED: group_id='{top['group_id']}', name='{top['name']}'"

    top_matches = matches.head(10)
    listing = "\n".join(
        f"  - group_id='{r['group_id']}', name={r['name']!r}"
        for _, r in top_matches.iterrows()
    )
    _record("resolve_client", f"⚠️ ambiguous '{name}' ({len(matches)} matches)")
    return (
        f"AMBIGUOUS: Found {len(matches)} clients matching {name!r}. "
        f"Show this list to the user and ask which one they meant:\n{listing}"
    )


# ---- SQL execution tools ----

@tool
def sql_db_query_checker(query: str) -> str:
    """Double-check an Athena SQL query for common Athena/Presto/Trino
    mistakes BEFORE executing. Returns the cleaned query (or original
    unchanged)."""
    # Use the same model the user selected in the sidebar (set by build_agent),
    # so a Bedrock-only / key-free run doesn't fail here on a missing
    # OPENAI_API_KEY. Falls back to the default choice if unset.
    checker = models.build_active_llm()

    trigger = f"""{query}

Double-check the above AWS Athena (Trino/Presto SQL dialect) query for
common mistakes:

- Hyphenated database names MUST be double-quoted:
  "tg-deltalake-bronze", "tg-deltalake-silver", "tg-master"
- Read-only SELECT only — NO INSERT/UPDATE/DELETE/DROP
- LIMIT clause must be present on aggregation/GROUP BY queries (default 100).
  Do NOT add a LIMIT to detail/listing queries that return individual rows.
- For mastermusic queries: status = 1 filter for active catalogue
- For mastermusic ingestion-date filters: use from_iso8601_timestamp(datetime_added)
- For music_streams_v3 / playactivity_v2: dw_reported_date partition filter REQUIRED
- For music_streams_v3 / playactivity_v2: group_id partition filter REQUIRED
- For playactivity_v2 (bronze) — standard stream filters MANDATORY:
    is_fetch = 'false'           (STRING — use 'false', not boolean false!)
    is_dupe_ex_guid = false      (BOOLEAN — use false, not 'false'!)
    play_type != 'Preview'
- For music_streams_v3: use SUM(is_stream) for stream counts, NOT COUNT(*)
- For fetch/download counts: ALWAYS use "tg-deltalake-silver"."music_fetches" — NEVER webhooklog or any bronze table.
  music_fetches filters: group_id (PARTITION) + date(dw_reported_date) = date('YYYY-MM-DD'). Use COUNT(*) not SUM.
- For arrays: CROSS JOIN UNNEST(col) AS t(x)
- For maps: element_at(col, 'KEY')
- Date functions: date_parse, from_iso8601_date, from_iso8601_timestamp

If there are mistakes, rewrite. Otherwise return unchanged.
Output ONLY the SQL — no markdown fences, no commentary.

SQL Query:"""

    resp = checker.invoke(trigger)
    # Not resp.content.strip(): the native Google client returns a list of
    # typed blocks, not a string, so .strip() raises AttributeError and takes
    # the whole run down. The normaliser flattens whatever shape arrives.
    text = models.message_text(resp.content).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    _record("sql_db_query_checker", "query reviewed")
    return text.strip()


@tool
def sql_db_query(query: str) -> str:
    """Execute a validated Athena SQL query. Returns success summary +
    preview, or 'ATHENA ERROR: <msg>' (in which case: read the error, fix
    the SQL, re-check, retry)."""
    if not current_run().dictionary_loaded:
        _record("sql_db_query", "🛑 blocked: dictionary not loaded")
        return ("QUERY NOT RUN - you have not loaded the data dictionary in this "
                "turn. Call get_data_dictionary(tables) first, naming every table "
                "this query touches (e.g. \"mastermusic, track_active\"), read it, "
                "then rewrite the query to follow it. The dictionary lists the real "
                "tables and columns; do not guess them.")
    blocked = check_query_allowed(query)
    if blocked:
        _record("sql_db_query", "\U0001f6d1 blocked: " + blocked.split(" - ")[0].split("\n")[0])
        return blocked

    if not current_run().execute_live:
        _record("sql_db_query", "⏭️ skipped (live off)")
        return ("EXECUTION SKIPPED: 'Execute against Athena' toggled off. "
                "Return the SQL to the user without running it.")
    try:
        record_query(query)
        df = _run_athena_query(query, limit_rows=100)  # preview; full data served from S3
        _stash_result(df)
        preview = df.head(10).to_string(index=False, max_cols=8)[:1500]
        _record("sql_db_query", f"✅ {len(df)} rows")
        return (
            f"Query succeeded. {len(df)} rows returned "
            f"(query_id={df.attrs.get('query_id')}).\n\n"
            f"Preview (first 10 rows):\n{preview}"
        )
    except (BotoCoreError, ClientError, RuntimeError, TimeoutError) as e:
        err = str(e)
        hint = ""
        if "OutputLocation" in err or "output location" in err.lower():
            hint = "\nHINT: Set S3 Output Location in the sidebar."
        elif "AccessDenied" in err:
            hint = "\nHINT: Check IAM permissions."
        elif "SSO" in err or "expired" in err.lower():
            hint = "\nHINT: Run `aws sso login --profile <profile>` and retry."
        elif "Column" in err and "cannot be resolved" in err:
            hint = "\nHINT: Use describe_table to confirm the exact column name."
        elif "Table" in err and ("not found" in err.lower() or "does not exist" in err.lower()):
            hint = "\nHINT: Use list_tables(db) to confirm the table exists."
        _record("sql_db_query", f"❌ {err[:120]}")
        return f"ATHENA ERROR: {err}{hint}"


# ---- UX tools ----

@tool
def visualize_results(chart_type: str, x_column: str, y_column: str, title: str = "") -> str:
    """Create a chart from the most recent query results. Call AFTER
    sql_db_query succeeds when a chart would add value.

    Args:
      chart_type: 'bar', 'line', 'pie', 'scatter', 'area'
      x_column: X axis column (or labels for pie)
      y_column: Y axis column (or values for pie)
      title: short title
    """
    run = current_run()
    df = run.dataframe
    if df is None or len(df) == 0:
        return "No data to visualize."
    if x_column not in df.columns:
        return f"Column {x_column!r} not in results. Available: {list(df.columns)}"
    if y_column not in df.columns:
        return f"Column {y_column!r} not in results. Available: {list(df.columns)}"
    if chart_type not in CHART_TYPES:
        return f"Invalid chart_type {chart_type!r}. Use one of: {list(CHART_TYPES)}"
    y = df[y_column]
    if pd.api.types.is_bool_dtype(y) or not pd.api.types.is_numeric_dtype(y):
        _record("visualize_results", f"skipped: {y_column!r} is not numeric")
        return (f"Not charted: {y_column!r} is not a numeric measure, so a chart "
                f"would show nothing useful. The table is the answer; don't chart it.")

    run.chart = {
        "type": chart_type, "x": x_column, "y": y_column,
        "title": title or f"{y_column} by {x_column}",
    }
    _record("visualize_results", f"📊 {chart_type}: {x_column} vs {y_column}")
    return f"Chart created: {chart_type} of '{y_column}' by '{x_column}'."


@tool
def note_default_applied(notice: str) -> str:
    """Inform the user when you've applied a default (e.g. month-to-date date
    range). The notice appears in the UI as an info banner.

    Example: note_default_applied("Defaulted to month-to-date: 2026-06-01 to today.")
    """
    _add_notice(notice)
    _record("note_default_applied", notice[:80])
    return f"Notice recorded: {notice}"


# ---- Progress text for end users ----

def friendly_status(tool_name: str) -> str:
    """A non-technical progress line for end users (dev mode off), keyed
    loosely off the tool name so it still reflects what the agent is doing."""
    t = (tool_name or "").lower()
    if "checker" in t:
        return "Double-checking the query…"
    if "schema" in t or "list" in t or "info" in t or "table" in t:
        return "Exploring the data catalogue…"
    if "entity" in t or "resolve" in t or "lookup" in t or "match" in t or "name" in t:
        return "Looking up the right names…"
    if "sql" in t or "query" in t or "execute" in t or "athena" in t:
        return "Running the query and gathering results…"
    return "Working on your question…"
