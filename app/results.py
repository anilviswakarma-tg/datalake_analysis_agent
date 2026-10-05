"""Turning a result DataFrame into what the user sees: chart selection and
CSV/Excel export bytes. Framework-free; rendering lives in the UI layer."""

from __future__ import annotations

import io
from typing import Dict, Optional

import pandas as pd



# ═══════════════════════════════════════════════════════════════════════════
# 9. CHART AUTO-SUGGESTION
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


CHART_TYPES = ("bar", "line", "pie", "scatter", "area")
# Tuned Global orange first; pie slices cycle through the rest.
CHART_COLORS = ["#E85420", "#F0A07F", "#8C8C8C", "#C8C8C8", "#5A5A5A", "#B8401A"]


def fmt_bytes(n: float) -> str:
    """Bytes for people: '840 B', '12.3 MB', '1.25 TB'."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def is_scalar_result(df: pd.DataFrame) -> bool:
    """A COUNT/SUM-style answer: one row of at most two columns, which the
    answer text already states, so the UI shows no table or downloads.
    A one-row lookup with more columns (a product's availability, say) is a
    record, and a user asking for it as a spreadsheet must still get one."""
    return len(df) <= 1 and len(df.columns) <= 2


def chart_frame(df: pd.DataFrame, chart: Dict[str, str]) -> pd.DataFrame:
    """The rows a chart plots: bar and pie keep the 25 largest, so a long tail
    doesn't turn the chart into noise; other types plot everything."""
    y = chart["y"]
    if chart["type"] in ("bar", "pie"):
        try:
            return df.nlargest(25, y) if pd.api.types.is_numeric_dtype(df[y]) else df.head(25)
        except Exception:
            return df.head(25)
    return df


def plotly_figure(df: pd.DataFrame, chart: Dict[str, str]):
    """A Plotly figure for a chart spec (from _auto_chart_spec or the
    visualize_results tool). Raises ValueError for an unknown chart type."""
    import plotly.express as px

    chart_type, x, y = chart["type"], chart["x"], chart["y"]
    data = chart_frame(df, chart)
    # Plotly Express colours traces when it builds them, so the palette has
    # to go in here; a later layout colorway would not repaint them.
    common = dict(title=chart.get("title", ""), color_discrete_sequence=CHART_COLORS)
    if chart_type == "bar":
        fig = px.bar(data, x=x, y=y, **common)
        fig.update_xaxes(type="category", categoryorder="total descending")
    elif chart_type == "line":
        fig = px.line(data, x=x, y=y, markers=True, **common)
    elif chart_type == "area":
        fig = px.area(data, x=x, y=y, **common)
    elif chart_type == "scatter":
        fig = px.scatter(data, x=x, y=y, **common)
    elif chart_type == "pie":
        fig = px.pie(data, names=x, values=y, hole=0.4, **common)
    else:
        raise ValueError(f"Unknown chart type: {chart_type}")
    fig.update_layout(
        template="plotly_dark", height=400,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


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
