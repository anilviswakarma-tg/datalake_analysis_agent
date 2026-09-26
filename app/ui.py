"""Presentation helpers shared across the app: styling, the agent catalogue,
per-agent conversation buckets, and rendering a completed run."""

from __future__ import annotations

import base64
import functools
import io
import os

import pandas as pd
import streamlit as st

import models
# Re-exported so display code can flatten message content without reaching
# into models; the rules belong to the provider, which models owns.
from models import (ContentNormalizer, GeminiNormalizer,  # noqa: F401
                    PassthroughNormalizer, active_normalizer, message_text,
                    normalizer_for)
from aws import _fetch_s3_csv
from config import LOGO_FILE, SCRIPT_DIR
from results import df_to_csv_bytes, df_to_excel_bytes, render_chart



@functools.lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    """Base64 data URI for the Tuned Global logo, embedded so it renders inside
    the custom login HTML without needing Streamlit static file serving."""
    try:
        raw = LOGO_FILE.read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def _inject_css(name: str) -> None:
    """Inject a stylesheet from assets/<name> as an inline <style> block.

    Kept in standalone .css files (assets/login.css, assets/app.css) rather
    than Python string constants so the styling is easy to read and diff.
    Read fresh on every run (not cached) so edits show up on a plain browser
    refresh during development."""
    try:
        css = (SCRIPT_DIR / "assets" / name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


# ── Domain "agents" shown in the sidebar, each with suggested questions ──────
_AGENTS = [
    {
        "key": "catalogue",
        "icon": ":material/library_music:",
        "label": "Master catalogue agent",
        "desc": "Track & product counts by label, territory and ingestion date.",
        "questions": [
            "How many tracks do we have from Sony?",
            "How many tracks do we have for China?",
            "How many products were ingested last week?",
        ],
    },
    {
        "key": "playlogs",
        "icon": ":material/play_circle:",
        "label": "Logs and Streams agent",
        "desc": "Play and fetch log volumes by client and time period.",
        "questions": [
            "How many play logs did we get from Etisalat last week?",
            "How many fetch logs did we get from Realize?",
        ],
    },
    {
        "key": "users",
        "icon": ":material/group:",
        "label": "Users and Subscriptions agent",
        "desc": "New users, subscriptions and playlist counts per client.",
        "questions": [
            "How many subscriptions were added to Gabb last week?",
            "How many new users were added to Etisalat last week?",
            "How many user playlists do we have for Gabb?",
        ],
    },
    {
        "key": "client_active",
        "icon": ":material/album:",
        "label": "Client catalogue agent",
        "desc": "Active catalogue sizes and label breakdowns per client.",
        "questions": [
            "How many active tracks do we have for Gabb?",
            "How many Orchard tracks are active in Etisalat?",
        ],
    },
]

_AGENT_KEYS = {a["key"] for a in _AGENTS}

# Chat runs are scoped per agent so switching agents in the sidebar shows only
# that agent's conversation (and each agent's follow-up context stays its own).
# A sentinel bucket holds any chat started while no agent is selected.
_GENERAL_BUCKET = "__general__"


def _runs_bucket_key() -> str:
    """The active conversation bucket, derived from the selected agent."""
    sel = st.session_state.get("selected_agent")
    return sel if sel in _AGENT_KEYS else _GENERAL_BUCKET


def _current_runs() -> list:
    """The run list for the active agent bucket (created lazily). Mutating the
    returned list (e.g. .append) persists; to empty a bucket use
    _clear_current_runs so the reassignment is stored back."""
    buckets = st.session_state.setdefault("runs_by_agent", {})
    return buckets.setdefault(_runs_bucket_key(), [])


def _clear_current_runs() -> None:
    """Reset just the active agent's conversation."""
    st.session_state.setdefault("runs_by_agent", {})[_runs_bucket_key()] = []


# App-wide styling for the authenticated pages (matches apis-playground).
def _friendly_status(tool_name: str) -> str:
    """A non-technical progress line for end users (developer mode off), keyed
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


def _render_answer(run: dict, i: int, dev: bool) -> None:
    """Render a completed run's answer + data/downloads. Shared by the dev-mode
    tabbed view and the plain end-user view. Query ids / S3 paths are shown
    only in developer mode."""
    for notice in run.get("notices", []):
        st.info(f"ℹ️ {notice}")
    st.markdown(run["answer"])

    if run.get("dataframe") is None:
        return

    df = run["dataframe"]
    if run.get("chart"):
        render_chart(df, run["chart"])

    # Single-row results (COUNT, SUM, scalar aggregates) — suppress table and
    # downloads; the AI text already gives the answer.
    is_single_row = len(df) == 1

    if not is_single_row:
        display_df = df.copy()
        display_df.index = range(1, len(display_df) + 1)
        display_df.index.name = "#"
        st.dataframe(display_df, use_container_width=True, height=320)

    query_id = run.get("query_id", "n/a")
    s3_base = os.getenv("ATHENA_OUTPUT_S3", "")

    # Fetch full S3 result once per query and cache it
    _s3_key = f"_s3_{query_id}"
    if _s3_key not in st.session_state and query_id != "n/a":
        st.session_state[_s3_key] = _fetch_s3_csv(query_id)
    s3_csv: bytes = st.session_state.get(_s3_key, b"")

    # Total row count from S3 line count (no re-parse needed)
    total_rows = max(s3_csv.count(b"\n") - 1, len(df)) if s3_csv else len(df)

    # Only show download buttons for multi-row results
    if not is_single_row:
        if total_rows > len(df):
            st.caption(
                f"⚠️ Showing first {len(df):,} of {total_rows:,} rows — "
                "CSV and Excel contain the full dataset."
            )
        if dev:
            st.caption(
                f"{total_rows:,} rows · query id `{query_id}`"
                + (f" · 📁 `{s3_base}{query_id}.csv`" if s3_base and query_id != "n/a" else "")
            )
        else:
            st.caption(f"{total_rows:,} rows")
        c1, c2 = st.columns(2)
        with c1:
            csv_data = s3_csv if s3_csv else df_to_csv_bytes(df)
            st.download_button(
                "⬇️ CSV", csv_data,
                file_name=f"datalake_{query_id}.csv",
                mime="text/csv", key=f"csv_{i}",
                use_container_width=True,
            )
        with c2:
            if s3_csv:
                try:
                    xl_data = df_to_excel_bytes(pd.read_csv(io.BytesIO(s3_csv)))
                except Exception:
                    xl_data = df_to_excel_bytes(df)
            else:
                xl_data = df_to_excel_bytes(df)
            st.download_button(
                "⬇️ Excel", xl_data,
                file_name=f"datalake_{query_id}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"xlsx_{i}",
                use_container_width=True,
            )
