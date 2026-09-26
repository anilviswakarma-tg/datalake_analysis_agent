"""Presentation helpers shared across the app: styling, the agent catalogue,
per-agent conversation buckets, and rendering a completed run."""

from __future__ import annotations

import base64
import functools
import io
import os
from typing import Dict

import pandas as pd
import streamlit as st

import models
# Re-exported so display code can flatten message content without reaching
# into models; the rules belong to the provider, which models owns.
from models import (ContentNormalizer, GeminiNormalizer,  # noqa: F401
                    PassthroughNormalizer, active_normalizer, message_text,
                    normalizer_for)
from aws import _fetch_s3_csv
from catalogue import AGENT_KEYS, AGENTS
from config import LOGO_FILE, SCRIPT_DIR
from tools import friendly_status as _friendly_status  # noqa: F401
from results import chart_frame, df_to_csv_bytes, df_to_excel_bytes



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
_AGENTS = AGENTS
_AGENT_KEYS = AGENT_KEYS

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


def render_chart(df: pd.DataFrame, chart: Dict[str, str]) -> None:
    import altair as alt
    chart_type, x, y = chart["type"], chart["x"], chart["y"]
    title = chart.get("title", "")
    is_auto = chart.get("auto", False)
    st.markdown(f"📊 **{title}**" + (" _(auto-generated)_" if is_auto else ""))

    df_plot = chart_frame(df, chart)

    try:
        if chart_type == "bar":
            c = alt.Chart(df_plot).mark_bar().encode(
                x=alt.X(f"{x}:N", sort="-y", title=x),
                y=alt.Y(f"{y}:Q", title=y),
                tooltip=list(df_plot.columns),
            ).properties(height=400)
        elif chart_type == "line":
            c = alt.Chart(df_plot).mark_line(point=True).encode(
                x=alt.X(f"{x}", title=x), y=alt.Y(f"{y}:Q", title=y),
                tooltip=list(df_plot.columns),
            ).properties(height=400)
        elif chart_type == "area":
            c = alt.Chart(df_plot).mark_area(opacity=0.6).encode(
                x=alt.X(f"{x}", title=x), y=alt.Y(f"{y}:Q", title=y),
                tooltip=list(df_plot.columns),
            ).properties(height=400)
        elif chart_type == "scatter":
            c = alt.Chart(df_plot).mark_circle(size=80).encode(
                x=alt.X(f"{x}:Q", title=x), y=alt.Y(f"{y}:Q", title=y),
                tooltip=list(df_plot.columns),
            ).properties(height=400)
        elif chart_type == "pie":
            c = alt.Chart(df_plot).mark_arc(innerRadius=60).encode(
                theta=alt.Theta(f"{y}:Q"),
                color=alt.Color(f"{x}:N", title=x),
                tooltip=list(df_plot.columns),
            ).properties(height=400)
        else:
            st.warning(f"Unknown chart type: {chart_type}")
            return
        st.altair_chart(c, use_container_width=True)
    except Exception as e:
        st.warning(f"Could not render chart: {e}")


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
