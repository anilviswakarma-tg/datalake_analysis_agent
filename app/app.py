"""Tuned Global Data Lake Agent - Streamlit entry point.

Run with:  streamlit run app.py

The application is split across small modules; see ARCHITECTURE.md for
the layout and dependency order. This file holds the page composition."""

from __future__ import annotations

import json
import os
import time

import streamlit as st
import streamlit.components.v1 as st_components

from agent import build_agent
from auth import _check_auth
from config import DATA_DICT_DIR, FEEDBACK_FILE, _openai_key_looks_real
from entities import _ENTITY_CACHE
from models import (_MODEL_REGISTRY, explain_failure, missing_key_reason,
                    model_label)
from results import fmt_bytes
from run_state import SessionLedger, start_run
from ui import (_AGENTS, _clear_current_runs, _current_runs, _friendly_status,
                _inject_css, _logo_data_uri, _render_answer, message_text)



def main():
    st.set_page_config(page_title="Data Lake Agent", page_icon="🎵", layout="wide")

    _check_auth()   # ← shows login screen and stops if not authenticated

    # Auto-logout: inject a JS timer that reloads the page when the session TTL
    # expires. Without this, an idle open tab would never trigger _check_auth()
    # again and the session would persist indefinitely.
    _SESSION_TTL = 24 * 60 * 60
    _remaining_ms = max(1000, int(
        (st.session_state.get("login_time", 0) + _SESSION_TTL - time.time()) * 1000
    ))
    st_components.html(
        f"<script>setTimeout(function(){{window.parent.location.reload();}},{_remaining_ms});</script>",
        height=0,
    )

    _inject_css("app.css")

    # Ensure AWS_PROFILE is never set inside the container — always use the
    # instance IAM role via the default credential chain.
    os.environ.pop("AWS_PROFILE", None)

    user_email = st.session_state.get("user_email", "")

    # ── Top bar: title · model dropdown · dev toggle · (Settings only in dev) ──
    tc1, tc2, tc3, tc4 = st.columns([5, 2.4, 1.9, 1.5], vertical_alignment="center")
    with tc1:
        st.markdown('<div class="tg-topbar-title">🎵 Data Lake Agent</div>', unsafe_allow_html=True)
    with tc2:
        model_choice = st.selectbox(
            "Model",
            options=list(_MODEL_REGISTRY.keys()),
            format_func=model_label,
            index=list(_MODEL_REGISTRY).index("glm"),
            key="model_choice",
            label_visibility="collapsed",
        )
    with tc3:
        dev_mode = st.toggle(
            "🛠️ Dev mode", value=False, key="dev_mode",
            help="On = show the agent's SQL, tool calls and message trace, plus the "
                 "⚙️ Settings menu. Off = clean end-user view with plain progress messages.",
        )

    # Settings menu is developer-only; end users never see it. Default to live
    # execution so hiding the menu can't leave dry-run switched on.
    execute_live = True
    if dev_mode:
        with tc4:
            with st.popover("⚙️ Settings", use_container_width=True):
                execute_live = st.toggle(
                    "Execute against Athena", value=True,
                    help="On = run live Athena queries. Off = show the generated SQL only (dry-run).",
                )
                st.divider()
                if st.button("🗑️ Clear conversation", use_container_width=True):
                    _clear_current_runs()
                    st.rerun()
                if st.button("♻️ Clear entity cache", use_container_width=True,
                             help="Forces label/client lookups to refresh from tg-master. Rarely needed."):
                    _ENTITY_CACHE.clear()
                    st.toast("Entity cache cleared.")

                st.divider()
                dict_files = (sorted(DATA_DICT_DIR.glob("*.md"))
                              if DATA_DICT_DIR.exists() else [])
                fb_ok = FEEDBACK_FILE.exists()
                st.caption(
                    f"Knowledge · data-dictionary "
                    f"{f'✅ {len(dict_files)} files' if dict_files else '❌ missing'}"
                    f" · feedback.md {'✅' if fb_ok else '—'}"
                )
                if fb_ok and st.button("📖 View feedback.md (admin)", use_container_width=True):
                    st.session_state.show_feedback = True
    st.divider()

    # ── Sidebar: agent navigation (playground-style) ────────────────────────
    with st.sidebar:
        _logo = _logo_data_uri()
        if _logo:
            st.markdown(
                f'<div class="tg-sb-logo"><img src="{_logo}" alt="Tuned Global"/></div>',
                unsafe_allow_html=True,
            )
        st.markdown('<div class="tg-sb-label">Agents</div>', unsafe_allow_html=True)
        _sel_now = st.session_state.get("selected_agent")
        for _agent in _AGENTS:
            if st.button(_agent["label"], icon=_agent["icon"],
                         key=f"nav_{_agent['key']}", use_container_width=True):
                st.session_state.selected_agent = _agent["key"]
                st.rerun()
        # Highlight the active nav item (accent-dim bg + orange inset bar)
        if _sel_now:
            st.markdown(
                f"<style>section[data-testid='stSidebar'] .st-key-nav_{_sel_now} button "
                f"{{ background: rgba(232,84,32,0.15) !important; color: rgb(212,212,212) !important; "
                f"box-shadow: inset 2px 0 0 #E85420 !important; }}</style>",
                unsafe_allow_html=True,
            )

        # Footer pinned to the bottom of the sidebar: signed-in user + sign out
        with st.container(key="sb_footer"):
            st.markdown(
                f'<div class="tg-sb-user" title="{user_email}">👤 {user_email}</div>',
                unsafe_allow_html=True,
            )
            if st.button("Sign out", key="sb_signout", use_container_width=True):
                method = st.session_state.get("auth_method", "password")
                st.session_state.authenticated = False
                st.session_state.user_email = ""
                st.session_state.auth_method = ""
                if method == "google":
                    st.logout()   # clears the OIDC identity cookie and reruns
                else:
                    st.rerun()

    if "runs_by_agent" not in st.session_state:
        st.session_state.runs_by_agent = {}
    # Conversation for the currently-selected agent only (see _current_runs).
    runs = _current_runs()

    # Show feedback file content in a modal-ish expander
    if st.session_state.get("show_feedback"):
        with st.expander("📖 knowledge/feedback.md", expanded=True):
            st.markdown(FEEDBACK_FILE.read_text(encoding="utf-8"))
            if st.button("Close"):
                st.session_state.show_feedback = False
                st.rerun()

    # Dry-run notice only (live is the default — no need for a persistent banner)
    if not execute_live:
        st.warning("🟡 **Dry-run mode** — the agent will generate SQL but not execute it. "
                   "Toggle it back on in ⚙️ Settings (top-right).")

    # Knowledge warning — the dictionary is the agent's only curated source
    if not DATA_DICT_DIR.exists() or not any(DATA_DICT_DIR.glob("*.md")):
        st.error(
            f"⚠️ Data dictionary missing or empty: `{DATA_DICT_DIR}`. "
            f"The agent will work via discovery only, and its answers will be "
            f"far less reliable."
        )

    # ---- Agent tiles / per-agent question tiles (main area) ----
    _agent_map = {a["key"]: a for a in _AGENTS}
    _sel = st.session_state.get("selected_agent")
    if _sel in _agent_map:
        _a = _agent_map[_sel]
        _navrow = st.columns([1.3, 2.0, 4.7], gap="small")
        with _navrow[0]:
            if st.button("‹  All agents", key="back_overview", use_container_width=True):
                st.session_state.selected_agent = None
                st.rerun()
        if runs:
            with _navrow[1]:
                if st.button("🗑️  Clear conversation", key="clear_conv_top",
                             use_container_width=True):
                    _clear_current_runs()
                    st.rerun()
        st.markdown(
            f'<div class="tg-section-title">{_a["label"]}</div>'
            '<div class="tg-section-sub">Pick a question to run, or type your own at the bottom.</div>',
            unsafe_allow_html=True,
        )
        with st.container(key="q_tiles"):
            _qcols = st.columns(2)
            for _j, _q in enumerate(_a["questions"]):
                with _qcols[_j % 2]:
                    if st.button(_q, icon=_a["icon"], key=f"qt_{_a['key']}_{_j}",
                                 use_container_width=True):
                        st.session_state.pending = _q
                        st.rerun()
    elif not runs:
        st.markdown(
            '<div class="tg-hero-badge">DATA LAKE AGENT</div>'
            '<div class="tg-hero-title">Ask your data lake anything</div>'
            '<div class="tg-hero-desc">Choose an agent below (or from the left sidebar) to see '
            'its suggested questions, or just type your own at the bottom.</div>',
            unsafe_allow_html=True,
        )
        with st.container(key="agent_tiles"):
            _acols = st.columns(2)
            for _i, _agent in enumerate(_AGENTS):
                with _acols[_i % 2]:
                    if st.button(f"**{_agent['label']}**\n\n{_agent['desc']}", icon=_agent["icon"],
                                 key=f"at_{_agent['key']}", use_container_width=True):
                        st.session_state.selected_agent = _agent["key"]
                        st.rerun()

    # ---- Render history ----
    for i, run in enumerate(runs):
        with st.chat_message("user"):
            st.markdown(run["question"])
        with st.chat_message("assistant"):
            if dev_mode:
                tab_answer, tab_trace, tab_messages = st.tabs(
                    ["💬 Answer", "🔧 Tool Trace", "📜 Agent Messages"]
                )
                with tab_answer:
                    _render_answer(run, i, dev_mode)
                with tab_trace:
                    if not run.get("trace"):
                        st.caption("No tool calls recorded.")
                    for step in run.get("trace", []):
                        st.markdown(f"**🔧 {step['tool']}** — {step['summary']}")
                with tab_messages:
                    for m in run.get("messages", []):
                        role = getattr(m, "type", "?")
                        content = getattr(m, "content", "")
                        tool_calls = getattr(m, "tool_calls", None) or []
                        name = getattr(m, "name", None)
                        with st.expander(f"[{role}]" + (f" {name}" if name else "")):
                            if content:
                                st.markdown(f"```\n{message_text(content)[:2000]}\n```")
                            if tool_calls:
                                for tc in tool_calls:
                                    st.markdown(f"→ **{tc['name']}**({json.dumps(tc.get('args', {}))[:200]})")
            else:
                _render_answer(run, i, dev_mode)

    # (Agent tiles & per-agent question tiles are rendered above, before history.)

    # ---- Input ----
    question = st.chat_input("Ask about catalogue, plays, or store metrics…")
    if "pending" in st.session_state:
        question = st.session_state.pop("pending")

    if question:
        # Check for a *usable* key, not merely a non-empty one: the .env.example
        # placeholder passes a truthiness test and then fails much later as a raw
        # 401 from inside the agent, which reads like an app bug rather than a
        # missing setting.
        _mcfg = _MODEL_REGISTRY.get(model_choice, {})
        if _mcfg.get("provider") == "openai" and not _openai_key_looks_real():
            st.error(
                "OpenAI API key not configured — set a real `OPENAI_API_KEY` in "
                "`.env` (the placeholder value doesn't work), then restart the app."
            )
            st.stop()
        # Same fail-fast for any OpenAI-compatible vendor (Gemini, Groq, …),
        # each of which names its own key env var in the registry.
        if (_key_problem := missing_key_reason(model_choice)) is not None:
            st.error(_key_problem)
            st.stop()
        # bedrock_converse and bedrock_mantle both use the EC2 IAM role — no key needed
        if execute_live and not os.getenv("ATHENA_OUTPUT_S3"):
            st.error("ATHENA_OUTPUT_S3 is not configured — set it in .env.")
            st.stop()

        # Per-run context: this user's question, model and live flag, and the
        # slot the tools write results into. Never a process-wide global.
        # One ledger per browser session: the repeat guard and scan budget
        # must survive Streamlit re-running the script, which re-asked the
        # same question 64 times on 2026-10-05. "Clear conversation" resets it.
        ledger = st.session_state.setdefault("ledger", SessionLedger())
        if ledger.over_budget():
            st.error(f"This session has scanned {fmt_bytes(ledger.bytes_scanned)} of "
                     f"Athena data, over its {fmt_bytes(ledger.allowance)} budget. "
                     "Use 🗑️ Clear conversation to start again.")
            st.stop()
        run_ctx = start_run(question=question, model_choice=model_choice,
                            execute_live=execute_live, session=ledger)

        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.status(
                "Agent thinking…" if dev_mode else "Working on your question…",
                expanded=dev_mode,
            ) as status:
                try:
                    agent = build_agent()
                    all_messages = []
                    final_answer = "(no answer)"
                    _streamed: list[str] = []
                    _answer_ph = st.empty()  # live-streams the final answer tokens

                    # Build conversation history from previous turns (capped at
                    # last 6 exchanges so we don't overflow the context window).
                    # History is drawn from the active agent's runs only, so one
                    # agent's Q&A never bleeds into another's context. The "Clear
                    # conversation" button resets that agent's runs (and history),
                    # giving the user an explicit way to start fresh.
                    _MAX_HISTORY = 6
                    history: list[dict] = []
                    for prev in runs[-_MAX_HISTORY:]:
                        history.append({"role": "user",      "content": prev["question"]})
                        history.append({"role": "assistant", "content": prev["answer"]})
                    history.append({"role": "user", "content": question})

                    # recursion_limit is the last line of defence; the
                    # query budget in run_state should stop a loop well before
                    # this trips.
                    for event in agent.stream(
                        {"messages": history},
                        config={"recursion_limit": 40},
                        stream_mode=["values", "messages"],
                    ):
                        mode, data = event
                        if mode == "values":
                            # Complete agent state after each step
                            msgs = data.get("messages", [])
                            if msgs:
                                all_messages = msgs
                                last = msgs[-1]
                                tcs = getattr(last, "tool_calls", None) or []
                                for tc in tcs:
                                    if dev_mode:
                                        args_preview = json.dumps(tc.get("args", {}))[:120]
                                        status.write(f"🔧 **{tc['name']}**({args_preview})")
                                    else:
                                        status.update(label=_friendly_status(tc["name"]))
                                if getattr(last, "type", None) == "ai" and not tcs:
                                    # Not `.content` directly: the native Google
                                    # client returns a list of typed blocks, which
                                    # would render as a raw Python repr.
                                    final_answer = message_text(last.content)
                        elif mode == "messages":
                            # Individual token chunks — stream the final answer live
                            # so the WebSocket never goes silent during LLM generation
                            msg_chunk, _ = data
                            # Flatten first: Gemini streams lists of typed
                            # blocks, which the isinstance(str) test below would
                            # reject — silently disabling live streaming and
                            # leaving the WebSocket quiet for the whole run.
                            chunk_content = message_text(getattr(msg_chunk, "content", ""))
                            is_tool = bool(
                                getattr(msg_chunk, "tool_calls", None)
                                or getattr(msg_chunk, "tool_call_chunks", None)
                            )
                            if (chunk_content
                                    and not is_tool
                                    and getattr(msg_chunk, "type", "") == "AIMessageChunk"):
                                _streamed.append(chunk_content)
                                _answer_ph.markdown("".join(_streamed) + " ▌")

                    _answer_ph.empty()  # cleared — final answer shown in chat history
                    status.update(label="Done", state="complete")

                    runs.append({
                        "question": question,
                        "answer": final_answer,
                        "dataframe": run_ctx.dataframe,
                        "query_id": run_ctx.query_id,
                        "trace": list(run_ctx.trace),
                        "chart": run_ctx.chart,
                        "notices": list(run_ctx.notices),
                        "messages": all_messages,
                    })
                    st.rerun()

                except Exception as e:
                    status.update(label="Failed", state="error")
                    # A quota or auth failure has a specific remedy, and telling
                    # the user to "try again" is wrong advice for both. Show the
                    # remedy when we recognise the failure; fall back otherwise.
                    explained = explain_failure(e)
                    if explained:
                        st.error(explained)
                        if dev_mode:
                            st.caption(f"Raw error: {e}")
                    elif dev_mode:
                        st.error(f"Agent failed: {e}")
                    else:
                        st.error("Sorry — something went wrong while answering. "
                                 "Please try again or rephrase your question.")


if __name__ == "__main__":
    main()
