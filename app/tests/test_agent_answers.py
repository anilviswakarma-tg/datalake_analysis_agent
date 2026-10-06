"""Questions the agent has answered wrongly, asked end to end: a real model,
real Athena. PENDING: skipped unless AGENT_LIVE_TESTS=1, as each one costs
model tokens and an Athena scan.

    AGENT_LIVE_TESTS=1 AGENT_TEST_MODEL=glm python -m pytest tests/test_agent_answers.py

AGENT_TEST_MODEL is a key of models._MODEL_REGISTRY (default gpt4o-mini).
Expected figures were measured directly in Athena on 2026-10-06;
track_active refreshes hourly, so a catalogue count can drift from them.

Status on GPT-4o mini (2026-10-06), with the country hint and dictionary
rules in place: Switzerland right; US right in one run and every country
(38,719,100) in the next; Egypt counted every country; "Egypt and Saudi
Arabia" resolved both as stores. Taken to be the model's limit, not a missing
rule, so no code guard was added. Re-run on a larger model.
"""
import os
import re

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_LIVE_TESTS") != "1",
    reason="pending: needs a live model and Athena (set AGENT_LIVE_TESTS=1)")

CASES = [
    # A worldwide track has only its WW row: ETEG has no US rows at all.
    ("How many active tracks does Etisalat have for the US currently?",
     ["7,134,333"]),
    # Egypt's own rows and the worldwide ones, as separate rows.
    ("How many active tracks does Etisalat have for Egypt currently?",
     ["31,462,583", "7,134,333"]),
    # Countries are not stores; one row per country plus WW.
    ("How many active tracks does Etisalat have for Egypt and Saudi Arabia?",
     ["31,462,583", "7,113,871", "7,134,333"]),
    # SPLH has no WW rows; COUNT(DISTINCT track_id), not its 693,602 rows.
    ("How many tracks does Spafax Lufthansa carry in Switzerland?",
     ["183,131"]),
    # Video is mastermusic.content_type, never asset_type_id.
    ("How many video streams did Etisalat have in September 2026?",
     ["23,449"]),
]


def _ask(question: str) -> str:
    from agent import build_agent
    from models import message_text
    from run_state import SessionLedger, start_run
    start_run(question=question, execute_live=True, session=SessionLedger(),
              model_choice=os.environ.get("AGENT_TEST_MODEL", "gpt4o-mini"))
    out = build_agent().invoke({"messages": [{"role": "user", "content": question}]},
                               config={"recursion_limit": 40})
    return message_text(out["messages"][-1].content)


@pytest.mark.parametrize("question,figures", CASES, ids=["eteg-us", "eteg-egypt", "eteg-egypt-saudi", "splh-switzerland", "eteg-video"])
def test_the_agent_answers(question, figures):
    answer = _ask(question)
    digits = re.sub(r"(?<=\d) (?=\d{3})", ",", answer)   # "7 134 333" -> "7,134,333"
    missing = [f for f in figures if f not in digits and f.replace(",", "") not in digits]
    assert not missing, f"missing {missing} in: {answer}"
