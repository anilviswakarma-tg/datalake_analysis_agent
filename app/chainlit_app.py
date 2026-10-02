"""Tuned Global Data Lake Agent - Chainlit entry point.

Run with:  chainlit run chainlit_app.py

Everything below the UI - agent, tools, run context, AWS, knowledge - is
shared with the Streamlit app and framework-free. This file maps it onto
Chainlit: auth callbacks, the model picker, settings, chat history and the
streamed agent run. The landing page with the four agents, and the rest of
the Streamlit-era layout, is drawn in the browser by public/app.js from
public/agents.json (generated from catalogue.py)."""

from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import os
from typing import Any, Dict, List, Optional

import chainlit as cl
import chainlit.server as cl_server
import pandas as pd
from chainlit.config import config as cl_config
from chainlit.data.acl import is_thread_author
from chainlit.input_widget import Switch
from chainlit.server import UserParam
from chainlit.utils import utc_now
from fastapi import HTTPException
from fastapi.responses import JSONResponse

import access
import chat_store
from agent import build_agent
from aws import _fetch_s3_csv
from chainlit_data import build_data_layer
from config import DATA_DICT_DIR, FEEDBACK_FILE, _openai_key_looks_real
from entities import _ENTITY_CACHE
from models import (_MODEL_REGISTRY, explain_failure, message_text,
                    missing_key_reason, model_label)
from results import (df_to_csv_bytes, df_to_excel_bytes, is_scalar_result,
                     plotly_figure)
from run_state import DEFAULT_MODEL_CHOICE, start_run
from tools import friendly_status

log = logging.getLogger(__name__)

# Deployed containers use the instance IAM role; a stray AWS_PROFILE there
# names a profile that doesn't exist and breaks credential resolution. Only
# drop it in a container, so a local run can still use a named profile.
if access.in_container():
    os.environ.pop("AWS_PROFILE", None)

# Chainlit serves public/ with the type Python guesses, and Windows has no
# entry for .woff2, so the fonts went out as application/octet-stream -
# a generic download that antivirus and web filters may hold for scanning.
mimetypes.add_type("font/woff2", ".woff2")

MAX_HISTORY = 25         # previous exchanges sent back to the model
RECURSION_LIMIT = 40     # last line of defence; run_state's query budget trips first


# ═══════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════

def _user(email: str, method: str) -> cl.User:
    return cl.User(identifier=email.strip().lower(), metadata={"auth_method": method})


@cl.password_auth_callback
async def password_auth(username: str, password: str) -> Optional[cl.User]:
    """Shared-password login: a @tunedglobal.com email plus APP_PASSWORD."""
    if access.password_login_ok(username, password):
        return _user(username, "password")
    return None


if os.getenv("OAUTH_GOOGLE_CLIENT_ID"):
    # Registered only when configured: Chainlit refuses to start with an
    # oauth_callback and no provider.
    @cl.oauth_callback
    async def oauth_login(provider_id: str, token: str, raw_user_data: Dict[str, str],
                          default_user: cl.User,
                          id_token: Optional[str] = None) -> Optional[cl.User]:
        # id_token defaults to None: some provider routes pass 4 arguments.
        email = raw_user_data.get("email", "")
        if (provider_id == "google" and raw_user_data.get("email_verified", False)
                and access.email_allowed(email)):
            return _user(email, "google")
        return None     # sends the user to Chainlit's sign-in error page


if access.dev_bypass_requested():
    if access.in_container():
        log.error("DEV_SKIP_AUTH is set on a containerised (deployed) host. "
                  "Ignoring it and requiring normal sign-in. Remove it from "
                  "the server's .env.")
    else:
        # LOCAL DEV ONLY. Header auth signs the browser in without a login
        # form. access.dev_bypass_email() re-checks the container gate on
        # every call, so this can't sign anyone in on a server.
        @cl.header_auth_callback
        async def dev_bypass(headers) -> Optional[cl.User]:
            email = access.dev_bypass_email()
            return _user(email, "dev-bypass") if email else None


# ═══════════════════════════════════════════════════════════════════════════
# CHAT HISTORY
# ═══════════════════════════════════════════════════════════════════════════
# Which database is chat_store's business (CHAT_DB_URL); nothing below knows
# or cares whether it is SQLite or Postgres. CHAT_DB_URL=off disables history.

CHAT_DB_URL = chat_store.chat_db_url()

RETENTION_SWEEP_SECONDS = 24 * 60 * 60


async def _retention_sweep() -> None:
    """Delete non-favourite chats idle past chat_store.retention_days(), now and
    then daily. A failed sweep is logged and retried the next day."""
    while True:
        try:
            removed = await chat_store.purge_expired(CHAT_DB_URL)
            if removed:
                log.info("chat retention: deleted %d non-favourite chats", removed)
        except Exception:
            log.exception("chat retention sweep failed")
        await asyncio.sleep(RETENTION_SWEEP_SECONDS)


if CHAT_DB_URL:
    @cl.on_app_startup
    async def create_history_schema():
        await chat_store.ensure_schema(CHAT_DB_URL)
        if chat_store.retention_days():
            asyncio.get_running_loop().create_task(_retention_sweep())

    @cl.data_layer
    def history_data_layer():
        return build_data_layer(CHAT_DB_URL)

    # Favourite chats, for public/app.js. POST only: Chainlit's catch-all page
    # route is GET, so a GET here would never be reached.
    async def _favourites_state(user: Any) -> JSONResponse:
        chats = await chat_store.favourite_chats(CHAT_DB_URL, user.identifier)
        return JSONResponse({"favourites": chats,
                             "retention_days": chat_store.retention_days()})

    async def list_favourite_chats(current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return await _favourites_state(current_user)

    async def set_favourite_chat(thread_id: str, payload: Dict[str, bool], current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        await is_thread_author(current_user.identifier, thread_id)   # 401/404 otherwise
        await chat_store.set_favourite(CHAT_DB_URL, thread_id, current_user.identifier,
                                       bool(payload.get("favourite")))
        return await _favourites_state(current_user)

    _root = cl_config.run.root_path or ""
    cl_server.app.add_api_route(f"{_root}/datalake/favourites", list_favourite_chats, methods=["POST"])
    cl_server.app.add_api_route(f"{_root}/datalake/favourites/{{thread_id}}", set_favourite_chat,
                                methods=["POST"])


# ═══════════════════════════════════════════════════════════════════════════
# MODEL PICKER, SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

# Each user's last-used model and dev-mode setting, so a new chat - which
# picking an agent starts - keeps them (the Streamlit controls held their
# value for the browser session). In memory: a restart falls back to the
# defaults, which is harmless. Dry-run is deliberately not carried over: a
# new chat always starts live, so nobody lands in dry-run by surprise.
_LAST_MODEL: Dict[str, str] = {}
_DEV_MODE: Dict[str, bool] = {}


def _user_key() -> Optional[str]:
    user = cl.user_session.get("user")
    return user.identifier if user else None


def _model_mode() -> cl.Mode:
    """The model picker, shown in the message composer."""
    current = _LAST_MODEL.get(_user_key() or "", DEFAULT_MODEL_CHOICE)
    return cl.Mode(id="model", name="Model", options=[
        cl.ModeOption(id=key, name=model_label(key), default=(key == current))
        for key in _MODEL_REGISTRY
    ])


async def _send_settings() -> Dict[str, Any]:
    return await cl.ChatSettings([
        Switch(id="dev_mode", label="Dev mode",
               initial=_DEV_MODE.get(_user_key() or "", False),
               description="Show the agent's tool calls, SQL and query ids, "
                           "and the developer commands."),
        Switch(id="execute_live", label="Execute against Athena", initial=True,
               description="Dev mode only. Off = generate SQL without running it (dry-run)."),
    ]).send()


# Developer commands, offered in the composer while dev mode is on.
_DEV_COMMANDS = [
    {"id": "clear-entity-cache", "icon": "refresh-cw", "button": False,
     "description": "Refresh label/client lookups from tg-master. Rarely needed."},
    {"id": "view-feedback", "icon": "book-open", "button": False,
     "description": "Show knowledge/feedback.md (the agent's captured findings)."},
]


def _settings() -> Dict[str, Any]:
    return cl.user_session.get("settings") or {}


def _dev_mode() -> bool:
    return bool(_settings().get("dev_mode"))


def _execute_live() -> bool:
    # Dry-run is a developer setting; with dev mode off, always run live so a
    # hidden toggle can't leave a user silently in dry-run.
    return bool(_settings().get("execute_live", True)) if _dev_mode() else True


async def _start_session(history: List[Dict[str, str]]) -> None:
    cl.user_session.set("history", history)
    cl.user_session.set("settings", await _send_settings())
    await cl.context.emitter.set_modes([_model_mode()])
    if _dev_mode():
        await cl.context.emitter.set_commands(_DEV_COMMANDS)


@cl.on_chat_start
async def on_chat_start():
    await _start_session([])

    # A toast, not a message: a message would hide the landing page. The
    # dev-bypass warning is a permanent banner instead (public/app.js, from
    # the user's auth_method), as in the Streamlit app.
    if not DATA_DICT_DIR.exists() or not any(DATA_DICT_DIR.glob("*.md")):
        await cl.context.emitter.send_toast(
            f"Data dictionary missing or empty ({DATA_DICT_DIR}). Answers will be "
            "far less reliable.", type="error")


@cl.on_chat_resume
async def on_chat_resume(thread: Dict[str, Any]):
    """Reopen a saved chat: rebuild the conversation the model sees and
    re-attach each answer's table, chart and downloads.

    Both come from the answer messages' saved metadata, not from the session
    Chainlit stores on disconnect, which a crash or restart skips."""
    history: List[Dict[str, str]] = []
    saved = []
    for step in thread.get("steps", []):
        if step.get("type") != "assistant_message":
            continue
        record = chat_store.read_answer_metadata(step.get("metadata"))
        if record:
            history.append({"question": record["question"], "answer": record["answer"]})
            saved.append((step["id"], record.get("result")))
    await _start_session(history)

    for step_id, result in saved:
        df = chat_store.preview_dataframe(result)
        if df is None:
            continue
        elements, _captions = await _result_parts(df, result.get("query_id"),
                                                  result.get("chart"), dev=_dev_mode())
        await _attach_to_resumed_thread(thread, step_id, elements)


async def _attach_to_resumed_thread(thread: Dict[str, Any], step_id: str,
                                    elements: List[cl.Element]) -> None:
    """Add elements to the thread Chainlit is about to send to the browser.

    Two Chainlit 2.12 quirks shape this (re-check both after an upgrade;
    tests/test_chat_store.py guards the first and the private _create):
    - on_chat_resume runs BEFORE the thread is sent, and the browser replaces
      its elements with the thread's on arrival - so element.send() from here
      is wiped. Elements must travel inside the thread.
    - the browser derives an element's URL from its chainlitKey for a live
      element but not for one inside a resumed thread, which it then drops.
      So the URL is set here, to the same session-and-user-checked file route.
    `_create` is private: it stores the content in the session without
    emitting anything."""
    session_id = cl.context.session.id
    root = (cl_config.run.root_path or "").rstrip("/")
    for element in elements:
        element.for_id = step_id
        await element._create(persist=False)
        element.url = f"{root}/project/file/{element.chainlit_key}?session_id={session_id}"
        thread.setdefault("elements", []).append(element.to_dict())


@cl.on_settings_update
async def on_settings_update(settings: Dict[str, Any]):
    cl.user_session.set("settings", settings)
    if key := _user_key():
        _DEV_MODE[key] = _dev_mode()            # carried into the next chat
    await cl.context.emitter.set_commands(_DEV_COMMANDS if _dev_mode() else [])
    if _dev_mode() and not _execute_live():
        await cl.context.emitter.send_toast(
            "Dry-run mode - the agent will generate SQL but not execute it.",
            type="warning")


async def _run_command(command: str) -> None:
    if not _dev_mode():
        return
    if command == "clear-entity-cache":
        _ENTITY_CACHE.clear()
        await cl.context.emitter.send_toast("Entity cache cleared.", type="success")
    elif command == "view-feedback":
        text = (FEEDBACK_FILE.read_text(encoding="utf-8") if FEEDBACK_FILE.exists()
                else "_feedback.md does not exist yet._")
        await cl.Message(content=f"**knowledge/feedback.md**\n\n{text}").send()


# ═══════════════════════════════════════════════════════════════════════════
# THE AGENT RUN
# ═══════════════════════════════════════════════════════════════════════════

def _selected_model(message: cl.Message) -> str:
    choice = (message.modes or {}).get("model") or DEFAULT_MODEL_CHOICE
    return choice if choice in _MODEL_REGISTRY else DEFAULT_MODEL_CHOICE


def _preflight_problem(model_choice: str, execute_live: bool) -> Optional[str]:
    """A setup problem to report instead of running, else None. Checks for a
    *usable* key, not merely a non-empty one: the .env.example placeholder
    would otherwise fail much later as a raw 401 that reads like an app bug."""
    if (_MODEL_REGISTRY[model_choice].get("provider") == "openai"
            and not _openai_key_looks_real()):
        return ("OpenAI API key not configured - set a real `OPENAI_API_KEY` in "
                "`.env` (the placeholder value doesn't work), then restart the app.")
    if (problem := missing_key_reason(model_choice)) is not None:
        return problem
    if execute_live and not os.getenv("ATHENA_OUTPUT_S3"):
        return "ATHENA_OUTPUT_S3 is not configured - set it in `.env`."
    return None


def _history_messages(question: str) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    for prev in (cl.user_session.get("history") or [])[-MAX_HISTORY:]:
        messages.append({"role": "user", "content": prev["question"]})
        messages.append({"role": "assistant", "content": prev["answer"]})
    messages.append({"role": "user", "content": question})
    return messages


async def _stream_agent(messages: List[Dict[str, str]], dev: bool):
    """Run the agent, streaming its tokens into a message and its tool calls
    into steps. Returns (final answer text, the message to finish).

    Text the model streams before a tool call is preamble ("Let me check...").
    It is dropped when the tool call starts, and a fresh message takes the
    tokens that follow, so the answer always renders below the steps."""
    agent = build_agent()
    answer = cl.Message(content="")
    # A Message picks up its parent from Chainlit's step context; a bare Step
    # does not, and the UI nests children under their parent while appending
    # parentless items at the end. So steps take the answer's parent, making
    # them siblings that render in the order they happen.
    parent_id = answer.parent_id
    final_answer = "(no answer)"
    tool_steps: Dict[str, cl.Step] = {}
    closed: set = set()

    # End users get one step whose label tracks progress in plain words;
    # dev mode gets a step per tool call, with arguments and output.
    progress: Optional[cl.Step] = None
    if not dev:
        progress = cl.Step(name="Working on your question…", type="run",
                           show_input=False, parent_id=parent_id)
        progress.start = utc_now()
        await progress.send()

    try:
        async for mode, data in agent.astream(
            {"messages": messages},
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode=["values", "messages"],
        ):
            if mode == "values":
                msgs = data.get("messages", [])
                if not msgs:
                    continue
                last = msgs[-1]
                tool_calls = getattr(last, "tool_calls", None) or []
                if tool_calls and answer.content:
                    await answer.remove()
                    answer = cl.Message(content="")
                for tc in tool_calls:
                    if tc["id"] in tool_steps:
                        continue
                    if dev:
                        step = cl.Step(name=tc["name"], type="tool", show_input="json",
                                       parent_id=parent_id)
                        step.input = tc.get("args", {})
                        step.start = utc_now()
                        await step.send()
                        tool_steps[tc["id"]] = step
                    else:
                        tool_steps[tc["id"]] = progress
                        progress.name = friendly_status(tc["name"])
                        await progress.update()
                if dev:
                    for m in msgs:
                        tc_id = getattr(m, "tool_call_id", None)
                        if (getattr(m, "type", None) == "tool" and tc_id in tool_steps
                                and tc_id not in closed):
                            step = tool_steps[tc_id]
                            step.output = message_text(m.content)[:4000]
                            step.end = utc_now()
                            await step.update()
                            closed.add(tc_id)
                if getattr(last, "type", None) == "ai" and not tool_calls:
                    # Not `.content` directly: the native Google client returns
                    # a list of typed blocks, which would render as a repr.
                    final_answer = message_text(last.content)

            elif mode == "messages":
                chunk, meta = data
                # Only the agent's own model node. The SQL checker tool makes
                # its own LLM call, whose tokens would otherwise stream into
                # the answer as raw SQL.
                if meta.get("langgraph_node") != "model":
                    continue
                if getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_chunks", None):
                    continue
                if getattr(chunk, "type", "") != "AIMessageChunk":
                    continue
                # Flatten first: Gemini streams lists of typed blocks.
                if text := message_text(getattr(chunk, "content", "")):
                    await answer.stream_token(text)
    except BaseException:
        if answer.content:
            await answer.remove()
        raise
    finally:
        if progress is not None:
            await progress.remove()

    return final_answer, answer


async def _result_parts(df: Optional[pd.DataFrame], query_id: Optional[str],
                        chart: Optional[Dict[str, Any]], dev: bool):
    """Caption lines and elements for a run's results. Used for a fresh
    answer and again, from the saved preview, when a chat is reopened."""
    elements: List[cl.Element] = []
    captions: List[str] = []
    if df is None:
        return elements, captions

    if chart:
        try:
            label = chart.get("title", "chart") + (" (auto-generated)" if chart.get("auto") else "")
            elements.append(cl.Plotly(name=label, figure=plotly_figure(df, chart),
                                      display="inline", size="large"))
        except Exception as e:           # a bad chart must not lose the answer
            captions.append(f"⚠️ Could not render chart: {e}")

    # Scalar results (COUNT, SUM): the answer text already gives the number,
    # so no table and no downloads.
    if is_scalar_result(df):
        return elements, captions

    elements.append(cl.Dataframe(name="Results", data=df, display="inline"))

    s3_csv: bytes = await cl.make_async(_fetch_s3_csv)(query_id) if query_id else b""
    total_rows = max(s3_csv.count(b"\n") - 1, len(df)) if s3_csv else len(df)

    def excel_bytes() -> bytes:
        if s3_csv:
            try:
                return df_to_excel_bytes(pd.read_csv(io.BytesIO(s3_csv)))
            except Exception:
                pass
        return df_to_excel_bytes(df)

    stem = f"datalake_{query_id or 'results'}"
    elements.append(cl.File(name=f"{stem}.csv", content=s3_csv or df_to_csv_bytes(df),
                            mime="text/csv", display="inline"))
    elements.append(cl.File(
        name=f"{stem}.xlsx", content=await cl.make_async(excel_bytes)(),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        display="inline"))

    if total_rows > len(df):
        captions.append(f"⚠️ Showing first {len(df):,} of {total_rows:,} rows - "
                        "CSV and Excel contain the full dataset.")
    if dev and query_id:
        s3_base = os.getenv("ATHENA_OUTPUT_S3", "")
        captions.append(f"{total_rows:,} rows · query id `{query_id}`"
                        + (f" · `{s3_base}{query_id}.csv`" if s3_base else ""))
    else:
        captions.append(f"{total_rows:,} rows")
    return elements, captions


def _failure_text(e: Exception, dev: bool) -> str:
    # A quota or auth failure has a specific remedy; "try again" is wrong
    # advice for both.
    if explained := explain_failure(e):
        return explained + (f"\n\nRaw error: `{e}`" if dev else "")
    if dev:
        return f"Agent failed: {e}"
    return ("Sorry - something went wrong while answering. "
            "Please try again or rephrase your question.")


@cl.on_message
async def on_message(message: cl.Message):
    if message.command:
        await _run_command(message.command)
        return
    question = (message.content or "").strip()
    if not question:
        return

    dev, live = _dev_mode(), _execute_live()
    model_choice = _selected_model(message)
    if key := _user_key():
        _LAST_MODEL[key] = model_choice
    if problem := _preflight_problem(model_choice, live):
        await cl.Message(content=f"⚠️ {problem}").send()
        return

    # This user's question, model and live flag, and the slot the tools write
    # results into. Scoped to this task's context, never process-wide.
    ctx = start_run(question=question, model_choice=model_choice, execute_live=live)
    try:
        final_answer, answer = await _stream_agent(_history_messages(question), dev)
    except Exception as e:
        log.exception("agent run failed")
        await cl.Message(content=_failure_text(e, dev)).send()
        return

    elements, captions = await _result_parts(ctx.dataframe, ctx.query_id, ctx.chart, dev)
    notices = "\n".join(f"> ℹ️ {n}" for n in ctx.notices)
    answer.content = "\n\n".join(p for p in (
        notices, final_answer, "\n".join(f"_{c}_" for c in captions)) if p)
    answer.elements = elements
    # Saved with the message, so a reopened chat can rebuild its context and
    # results without any file storage (see chat_store).
    answer.metadata = chat_store.answer_metadata(
        question, final_answer, ctx.dataframe, ctx.query_id, ctx.chart, ctx.notices)
    await answer.send()

    cl.user_session.get("history").append({"question": question, "answer": final_answer})
