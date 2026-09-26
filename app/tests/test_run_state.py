"""The per-run context must be per run, not per process.

It used to be a module-level dict (plus an os.environ flag and a models
global), so two users asking at once overwrote each other's results, query
budget, dry-run setting and model choice. These tests pin the replacement:
a ContextVar holding a RunContext, which the agent's tools must still reach
when LangGraph runs them on worker threads.
"""
import asyncio
import inspect
import threading

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import run_state


def _in_fresh_thread(fn):
    """Run fn in a new thread, which starts with an empty context."""
    out = {}

    def target():
        try:
            out["value"] = fn()
        except Exception as e:          # surfaced to the caller below
            out["error"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join()
    return out


def test_no_active_run_raises_rather_than_silently_creating_one():
    out = _in_fresh_thread(run_state.current_run)
    assert isinstance(out.get("error"), RuntimeError)
    assert _in_fresh_thread(run_state.active_run)["value"] is None


def test_model_choice_defaults_outside_a_run():
    import models
    assert _in_fresh_thread(models.active_model_choice)["value"] == run_state.DEFAULT_MODEL_CHOICE


def test_concurrent_runs_do_not_share_state():
    """Two users' runs interleaved on two threads keep their own everything."""
    barrier = threading.Barrier(2)

    def user(name, model, live):
        ctx = run_state.start_run(question=name, model_choice=model, execute_live=live)
        barrier.wait()                  # both runs now exist at once
        run_state._record("tool", name)
        run_state.record_query(f"SELECT '{name}'")
        run_state._add_notice(name)
        barrier.wait()
        return ctx

    results = {}
    threads = [
        threading.Thread(target=lambda: results.__setitem__("a", user("a", "glm", True))),
        threading.Thread(target=lambda: results.__setitem__("b", user("b", "gemini", False))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name, model, live in (("a", "glm", True), ("b", "gemini", False)):
        ctx = results[name]
        assert ctx.question == name and ctx.model_choice == model and ctx.execute_live is live
        assert [s["summary"] for s in ctx.trace] == [name]
        assert ctx.notices == [name]
        assert ctx.executed_sql == [f"select '{name}'"]


class _ToolCallingFake(GenericFakeChatModel):
    """GenericFakeChatModel replays scripted messages; create_agent also needs
    bind_tools, which the fake doesn't implement."""

    def bind_tools(self, tools, **kwargs):
        return self


def _scripted_agent():
    """An agent whose model calls two tools in ONE turn - so LangGraph runs
    them concurrently on worker threads - then answers."""
    from langchain.agents import create_agent
    from tools import note_default_applied, sql_db_query

    model = _ToolCallingFake(messages=iter([
        AIMessage(content="", tool_calls=[
            {"name": "note_default_applied", "args": {"notice": "month to date"}, "id": "1"},
            {"name": "sql_db_query", "args": {"query": "SELECT 1"}, "id": "2"},
        ]),
        AIMessage(content="done"),
    ]))
    return create_agent(model, [note_default_applied, sql_db_query])


def _assert_tools_wrote_to(ctx):
    assert ctx.notices == ["month to date"]
    tools_called = {s["tool"] for s in ctx.trace}
    assert tools_called == {"note_default_applied", "sql_db_query"}
    # execute_live=False must be honoured from the run, not os.environ
    assert any("skipped" in s["summary"] for s in ctx.trace)


def test_tool_writes_reach_the_caller_through_a_sync_agent_run():
    ctx = run_state.start_run(question="q", execute_live=False)
    agent = _scripted_agent()
    list(agent.stream({"messages": [{"role": "user", "content": "q"}]},
                      stream_mode="values"))
    _assert_tools_wrote_to(ctx)


def test_tool_writes_reach_the_caller_through_an_async_agent_run():
    """The Chainlit path: astream from an event loop, sync tools in executors."""
    async def go():
        ctx = run_state.start_run(question="q", execute_live=False)
        agent = _scripted_agent()
        async for _ in agent.astream({"messages": [{"role": "user", "content": "q"}]},
                                     stream_mode="values"):
            pass
        return ctx

    _assert_tools_wrote_to(asyncio.run(go()))


@pytest.mark.parametrize("module", ["run_state", "tools", "results", "models", "agent"])
def test_core_modules_do_not_import_streamlit(module):
    """The agent side must stay framework-free so the UI can be swapped."""
    import importlib
    src = inspect.getsource(importlib.import_module(module))
    assert "import streamlit" not in src
    assert "st.session_state" not in src
