"""Entity resolution: mapping human names onto the owner_id / group_id
values that every table is actually keyed on."""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

from run_state import tracked_query
from config import _master_db



# ═══════════════════════════════════════════════════════════════════════════
# 5. ENTITY RESOLUTION (15-min in-memory cache)
# ═══════════════════════════════════════════════════════════════════════════

_ENTITY_CACHE: Dict[str, Tuple[float, pd.DataFrame]] = {}
_CACHE_TTL_SECS = 15 * 60


def _cache_get(key: str) -> Optional[pd.DataFrame]:
    entry = _ENTITY_CACHE.get(key)
    if entry is None:
        return None
    ts, df = entry
    if time.time() - ts > _CACHE_TTL_SECS:
        del _ENTITY_CACHE[key]
        return None
    return df


def _cache_set(key: str, df: pd.DataFrame) -> None:
    _ENTITY_CACHE[key] = (time.time(), df)


def _load_musicowners() -> pd.DataFrame:
    cached = _cache_get("musicowners")
    if cached is not None:
        return cached
    sql = f'''
        SELECT id, name, display_name, active
        FROM "{_master_db()}"."musicowners"
        WHERE active = 1
    '''
    df = tracked_query(sql, "entity lookup", database=_master_db(), limit_rows=50000)
    _cache_set("musicowners", df)
    return df


def _load_groups() -> pd.DataFrame:
    cached = _cache_get("groups")
    if cached is not None:
        return cached
    sql = f'''
        SELECT group_id, name, country
        FROM "{_master_db()}"."groups"
    '''
    df = tracked_query(sql, "entity lookup", database=_master_db(), limit_rows=50000)
    _cache_set("groups", df)
    return df


def _fuzzy_match(df: pd.DataFrame, query: str, name_cols: List[str]) -> pd.DataFrame:
    q = query.strip().lower()
    if not q or len(df) == 0:
        return df.iloc[0:0]

    def score_row(row) -> int:
        best = 0
        for col in name_cols:
            val = str(row.get(col) or "").lower()
            if not val:
                continue
            if val == q:
                best = max(best, 100)
            elif val.startswith(q):
                best = max(best, 80)
            elif q in val:
                best = max(best, 60)
            else:
                q_tokens = set(q.split())
                v_tokens = set(val.split())
                if q_tokens & v_tokens:
                    best = max(best, 40)
        return best

    scored = df.copy()
    scored["score"] = scored.apply(score_row, axis=1)
    return scored[scored["score"] > 0].sort_values("score", ascending=False)
