"""Turning a result DataFrame into what the user sees: chart selection,
chart rendering, and CSV/Excel export bytes."""

from __future__ import annotations

import io
from typing import Dict, Optional

import pandas as pd
import streamlit as st



# ═══════════════════════════════════════════════════════════════════════════
# 9. CHART AUTO-SUGGESTION + RENDERING
# ═══════════════════════════════════════════════════════════════════════════

def _auto_chart_spec(df: pd.DataFrame) -> Optional[Dict[str, str]]:
    if df is None or len(df) < 2:
        return None
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    non_numeric_cols = [c for c in df.columns if c not in numeric_cols]
    if not numeric_cols:
        return None

    def _is_id_like(col: str) -> bool:
        lower = col.lower()
        return lower.endswith("_id") or lower == "id" or lower.startswith("id_")

    interesting_numeric = [c for c in numeric_cols if not _is_id_like(c)]
    if not interesting_numeric:
        return None

    try:
        y_col = max(interesting_numeric, key=lambda c: df[c].std() or 0)
    except Exception:
        y_col = interesting_numeric[0]

    if non_numeric_cols:
        def _x_score(col: str) -> int:
            lower = col.lower()
            if any(k in lower for k in ("name", "title", "label", "owner", "genre", "artist")):
                return 3
            if any(k in lower for k in ("date", "year", "month", "time")):
                return 2
            if any(k in lower for k in ("type", "category", "status", "language", "country", "group_id")):
                return 1
            return 0
        x_col = max(non_numeric_cols, key=_x_score)
        chart_type = "line" if any(k in x_col.lower() for k in ("date", "year", "month", "time")) else "bar"
    elif len(interesting_numeric) >= 2:
        x_col = interesting_numeric[0] if interesting_numeric[0] != y_col else interesting_numeric[1]
        chart_type = "scatter"
    else:
        return None

    return {
        "type": chart_type, "x": x_col, "y": y_col,
        "title": f"{y_col} by {x_col}", "auto": True,
    }


def render_chart(df: pd.DataFrame, chart: Dict[str, str]) -> None:
    import altair as alt
    chart_type, x, y = chart["type"], chart["x"], chart["y"]
    title = chart.get("title", "")
    is_auto = chart.get("auto", False)
    st.markdown(f"📊 **{title}**" + (" _(auto-generated)_" if is_auto else ""))

    if chart_type in ("bar", "pie"):
        try:
            df_plot = df.nlargest(25, y) if pd.api.types.is_numeric_dtype(df[y]) else df.head(25)
        except Exception:
            df_plot = df.head(25)
    else:
        df_plot = df

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


# ═══════════════════════════════════════════════════════════════════════════
# 10. EXPORT HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Results")
    return buf.getvalue()
