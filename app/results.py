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
