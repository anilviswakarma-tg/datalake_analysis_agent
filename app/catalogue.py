"""The domain "agents" offered on the landing page, each with suggested
questions. Framework-free: the UI decides how to present them.

All four share one underlying agent and prompt; the split is a way into the
data for non-technical users, and keeps each agent's conversation separate.
"""

from __future__ import annotations

import json
from pathlib import Path

AGENTS = [
    {
        "key": "catalogue",
        "icon": ":material/library_music:",   # Material Symbols, in both UIs
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

AGENT_KEYS = {a["key"] for a in AGENTS}

# The Chainlit page draws the landing tiles and sidebar list in the browser
# (public/app.js), so it reads the catalogue as a static file. Regenerate it
# after editing AGENTS:  python catalogue.py   (a test fails if it drifts).
UI_CATALOGUE_FILE = Path(__file__).parent / "public" / "agents.json"


def ui_catalogue() -> list:
    """AGENTS as the browser needs them: the bare Material icon name."""
    return [{"key": a["key"], "icon": a["icon"].removeprefix(":material/").rstrip(":"),
             "label": a["label"], "desc": a["desc"], "questions": a["questions"]}
            for a in AGENTS]


def ui_catalogue_json() -> str:
    return json.dumps(ui_catalogue(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    UI_CATALOGUE_FILE.write_text(ui_catalogue_json(), encoding="utf-8")
    print(f"wrote {UI_CATALOGUE_FILE}")
