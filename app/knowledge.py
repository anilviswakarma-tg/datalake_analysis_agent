"""Reading the curated knowledge files: the data dictionary (authoritative
per-table reference) and feedback.md."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import (DATA_DICT_DIR, DATA_DICT_README,
                    FEEDBACK_FILE, KNOWLEDGE_DIR)



# ═══════════════════════════════════════════════════════════════════════════
# 4. KNOWLEDGE FILE I/O
# ═══════════════════════════════════════════════════════════════════════════

def _normalise_table_name(raw: str) -> str:
    """Reduce whatever the agent passed to a bare lowercase table name.

    Accepts `mastermusic`, `"tg-deltalake-bronze"."mastermusic"`,
    `tg-deltalake-bronze.mastermusic`, etc.
    """
    name = raw.strip().strip("`").strip()
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name.strip().strip('"').strip("'").strip().lower()


def _data_dict_index() -> Dict[str, str]:
    """Map table name -> doc filename, parsed from the dictionary README's
    `## Tables` index.

    Parsed fresh on every call (the README is ~4KB) so dropping in a newer
    copy of the dictionary takes effect without restarting the app. Only the
    `## Tables` section is scanned — the `## Shared vocabulary` table below it
    also has backticked names and doc links, and would otherwise register
    column names like `group_id` as if they were tables.

    Falls back to the directory's filenames if the README is missing or its
    index can't be parsed, so the tool still works on a partial copy.
    """
    index: Dict[str, str] = {}
    try:
        readme = DATA_DICT_README.read_text(encoding="utf-8")
    except OSError:
        readme = ""

    in_tables = False
    for line in readme.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_tables = stripped.lower().startswith("## tables")
            continue
        if not in_tables or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        link = re.search(r"\]\(([^)]+\.md)\)", stripped)
        if not cells or not link:
            continue
        for name in re.findall(r"`([^`]+)`", cells[0]):
            index[_normalise_table_name(name)] = link.group(1)

    if not index:
        try:
            for f in sorted(DATA_DICT_DIR.glob("*.md")):
                if f.name.lower() != "readme.md":
                    index[f.stem.lower()] = f.name
        except OSError:
            pass
    return index


def _data_dict_preamble() -> str:
    """The README from `## Shared vocabulary` onward — shared ID vocabulary and
    the rules that apply across every table. The `## Tables` index above it is
    dropped: the agent has already been handed the docs it asked for, so the
    index is just tokens."""
    try:
        readme = DATA_DICT_README.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "## Shared vocabulary"
    idx = readme.find(marker)
    return readme[idx:].strip() if idx != -1 else ""


def _append_feedback(domain: str, observation: str, question: Optional[str] = None) -> None:
    """Append a timestamped entry to feedback.md."""
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    if not FEEDBACK_FILE.exists():
        FEEDBACK_FILE.write_text(
            "# Agent Feedback Log\n\n"
            "<!-- The agent appends new entries below this line -->\n",
            encoding="utf-8",
        )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = (
        f"\n---\n\n"
        f"**{ts}** — domain: `{domain}`\n\n"
    )
    if question:
        entry += f"_Question:_ {question}\n\n"
    entry += f"_Observation:_ {observation}\n"

    with FEEDBACK_FILE.open("a", encoding="utf-8") as f:
        f.write(entry)
