"""The Chainlit layer and the framework-free pieces it was built on.

No server, no LLM, no AWS: these check the rules (who may sign in), the
shared catalogue, the Plotly chart builder, and that the entry point wires
the callbacks it relies on.
"""
import inspect

import pandas as pd
import pytest

import access
import results


# ── access: who may use the app ─────────────────────────────────────────────

@pytest.mark.parametrize("email,ok", [
    ("someone@tunedglobal.com", True),
    ("  Someone@TunedGlobal.com ", True),
    ("someone@tunedglobal.com.evil.io", False),
    ("someone@nottunedglobal.com", False),
    ("", False),
    (None, False),
])
def test_email_domain_rule(email, ok):
    assert access.email_allowed(email) is ok


def test_password_login(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    assert access.password_login_ok("a@tunedglobal.com", "s3cret")
    assert not access.password_login_ok("a@tunedglobal.com", "wrong")
    assert not access.password_login_ok("a@gmail.com", "s3cret")


def test_unset_app_password_refuses_everyone(monkeypatch):
    """An empty APP_PASSWORD must not mean 'an empty password works'."""
    monkeypatch.setenv("APP_PASSWORD", "")
    assert not access.password_login_ok("a@tunedglobal.com", "")


@pytest.mark.parametrize("flag,in_container,expected", [
    ("true", False, "dev@tunedglobal.com"),
    ("TRUE ", False, "dev@tunedglobal.com"),
    ("true", True, None),       # a deployed host carrying the flag
    ("1", False, None),
    ("", False, None),
])
def test_dev_bypass_gate(monkeypatch, flag, in_container, expected):
    monkeypatch.setenv("DEV_SKIP_AUTH", flag)
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@tunedglobal.com")
    monkeypatch.setattr(access, "in_container", lambda: in_container)
    assert access.dev_bypass_email() == expected


def test_streamlit_auth_shares_the_rules():
    """Both UIs must enforce the same domain and container gate."""
    import auth
    assert auth._ALLOWED_DOMAIN == access.ALLOWED_DOMAIN
    assert auth._in_container is access.in_container


# ── catalogue ───────────────────────────────────────────────────────────────

def test_every_agent_is_complete():
    from catalogue import AGENTS
    for a in AGENTS:
        for field in ("key", "icon", "label", "desc", "questions"):
            assert a.get(field), f"{a.get('key')} missing {field}"
        assert a["icon"].startswith(":material/"), a["key"]


def test_browser_catalogue_is_up_to_date():
    """public/agents.json feeds the Chainlit landing page. Regenerate with
    `python catalogue.py` after editing catalogue.AGENTS."""
    import catalogue
    on_disk = catalogue.UI_CATALOGUE_FILE.read_text(encoding="utf-8")
    assert on_disk == catalogue.ui_catalogue_json(), "run: python catalogue.py"


# ── charts ──────────────────────────────────────────────────────────────────

DF = pd.DataFrame({"label": [f"L{i}" for i in range(30)], "tracks": range(30)})


@pytest.mark.parametrize("chart_type", results.CHART_TYPES)
def test_every_chart_type_builds(chart_type):
    x, y = ("tracks", "tracks") if chart_type == "scatter" else ("label", "tracks")
    fig = results.plotly_figure(DF, {"type": chart_type, "x": x, "y": y, "title": "t"})
    assert fig.data, chart_type


def test_bar_charts_keep_the_top_25_in_brand_colour():
    fig = results.plotly_figure(DF, {"type": "bar", "x": "label", "y": "tracks"})
    assert len(fig.data[0].x) == 25
    assert fig.data[0].marker.color == results.CHART_COLORS[0]


def test_unknown_chart_type_raises():
    with pytest.raises(ValueError):
        results.plotly_figure(DF, {"type": "radar", "x": "label", "y": "tracks"})


def test_visualize_tool_accepts_exactly_the_buildable_types():
    import run_state
    import tools
    ctx = run_state.start_run()
    ctx.dataframe = DF
    for t in results.CHART_TYPES:
        assert "Chart created" in tools.visualize_results.func(t, "label", "tracks")
    assert "Invalid" in tools.visualize_results.func("radar", "label", "tracks")


# ── the Chainlit entry point ────────────────────────────────────────────────

@pytest.fixture
def cl_app(monkeypatch):
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    import importlib
    import chainlit_app
    return importlib.reload(chainlit_app)


def test_chainlit_callbacks_are_registered(cl_app):
    from chainlit.config import config
    code = config.code
    for hook in ("on_message", "on_chat_start", "on_settings_update",
                 "password_auth_callback"):
        assert getattr(code, hook) is not None, hook


def test_new_chat_keeps_the_users_last_model(cl_app, monkeypatch):
    """The Streamlit model picker kept its value; a new chat must too."""
    import chainlit as cl
    from models import _MODEL_REGISTRY
    other = next(k for k in _MODEL_REGISTRY if k != cl_app.DEFAULT_MODEL_CHOICE)
    monkeypatch.setattr(cl_app, "_user_key", lambda: "a@tunedglobal.com")

    def default_of(mode):
        return [o.id for o in mode.options if o.default]

    assert default_of(cl_app._model_mode()) == [cl_app.DEFAULT_MODEL_CHOICE]
    cl_app._LAST_MODEL["a@tunedglobal.com"] = other
    assert default_of(cl_app._model_mode()) == [other]
    assert "_LAST_MODEL[key] = model_choice" in inspect.getsource(cl_app._answer)


def test_chainlit_run_starts_a_run_context(cl_app):
    """on_message must scope the run with start_run(), never a global."""
    src = inspect.getsource(cl_app._answer)
    assert "start_run(question=question, model_choice=model_choice" in src
    assert "_preflight_problem(" in src
    assert src.index("_preflight_problem(") < src.index("start_run(")


def test_chainlit_streams_only_the_model_node(cl_app):
    """The SQL checker's own LLM call must not stream into the answer."""
    src = inspect.getsource(cl_app._stream_agent)
    assert 'meta.get("langgraph_node") != "model"' in src
    assert "message_text(" in src


def test_chainlit_does_not_pop_aws_profile_on_a_dev_machine(cl_app):
    src = inspect.getsource(cl_app)
    assert 'if access.in_container():\n    os.environ.pop("AWS_PROFILE"' in src


def test_chainlit_session_timeout_matches_access_ttl():
    import tomllib
    from config import SCRIPT_DIR
    cfg = tomllib.loads((SCRIPT_DIR / ".chainlit" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["project"]["user_session_timeout"] == access.SESSION_TTL_SECONDS
    # SQLite can't store the tag list the SQLAlchemy data layer writes
    assert cfg["features"]["auto_tag_thread"] is False


# ── the browser layer (public/app.js, public/app.css) ───────────────────────
# They reproduce the Streamlit layout by hooking onto element ids and a few
# semantic class names in Chainlit's bundled frontend. A Chainlit upgrade
# that renames one breaks the page silently, so check they still exist.

def _frontend_bundle() -> str:
    from pathlib import Path
    import chainlit
    assets = Path(chainlit.__file__).parent / "frontend" / "dist" / "assets"
    return "".join(p.read_text(encoding="utf-8", errors="ignore")
                   for p in assets.glob("index-*.js"))


@pytest.mark.parametrize("hook", [
    '"welcome-screen"', '"header"', '"chat-input"', '"chat-submit"',
    '"new-chat-button"', '"thread-history"', '"readme-button"', '"theme-toggle"',
    '"user-nav-button"', "mode-picker-trigger-", '"data-sidebar":"sidebar"',
    "message-content", "inline-plotly-container", '"thread-options"', '"rename-thread"',
    "mode-picker-wrapper",
])
def test_chainlit_frontend_still_has_what_app_js_hooks_onto(hook):
    assert hook in _frontend_bundle(), hook


def test_dev_mode_carries_into_new_chats_but_dry_run_does_not(cl_app, monkeypatch):
    """Picking an agent starts a new chat; dev mode used to reset each time."""
    import asyncio
    sent = {}

    class FakeSettings:
        def __init__(self, inputs):
            sent["initial"] = {i.id: i.initial for i in inputs}

        async def send(self):
            return dict(sent["initial"])

    monkeypatch.setattr(cl_app, "_user_key", lambda: "a@tunedglobal.com")
    monkeypatch.setattr(cl_app.cl, "ChatSettings", FakeSettings)
    monkeypatch.setattr(cl_app, "_DEV_MODE", {})
    asyncio.run(cl_app._send_settings())
    assert sent["initial"] == {"dev_mode": False, "execute_live": True}
    cl_app._DEV_MODE["a@tunedglobal.com"] = True
    asyncio.run(cl_app._send_settings())
    assert sent["initial"] == {"dev_mode": True, "execute_live": True}
    src = inspect.getsource(cl_app.on_settings_update)
    assert "_DEV_MODE[key] = _dev_mode()" in src
    assert "_DEV_MODE[key] = _execute_live" not in src      # dry-run stays per chat


def test_favourite_routes_are_post_and_signed_in(cl_app):
    """GET would lose to Chainlit's catch-all page route, and both must
    require a signed-in user; setting a favourite also checks the chat's owner."""
    import chainlit.server as server
    routes = {(r.path, tuple(sorted(r.methods))) for r in server.app.routes
              if getattr(r, "path", "").endswith(("/datalake/favourites", "/datalake/favourites/{thread_id}"))}
    assert {m for _, m in routes} == {("POST",)}
    assert len({p for p, _ in routes}) == 2
    src = inspect.getsource(cl_app.set_favourite_chat)
    assert "is_thread_author(current_user.identifier, thread_id)" in src
    assert src.index("is_thread_author") < src.index("set_favourite(")


def test_the_favourites_cap_is_reported_not_raised(cl_app, monkeypatch):
    """At the cap the route answers 409 with words for the user (app.js
    shows them), and the page learns the cap from the favourites state."""
    import asyncio
    from types import SimpleNamespace

    async def owner(*a):
        return True

    async def at_cap(*a):
        raise cl_app.chat_store.FavouriteLimitReached(20)
    monkeypatch.setattr(cl_app, "is_thread_author", owner)
    monkeypatch.setattr(cl_app.chat_store, "set_favourite", at_cap)
    response = asyncio.run(cl_app.set_favourite_chat(
        "t1", {"favourite": True}, SimpleNamespace(identifier="a@tunedglobal.com")))
    assert response.status_code == 409
    assert b"You can keep up to 20 favourite chats" in response.body
    assert '"max_favourites": chat_store.favourites_max()' in inspect.getsource(cl_app._favourites_state)


def test_fonts_are_served_as_fonts(cl_app):
    import mimetypes
    assert mimetypes.guess_type("x.woff2")[0] == "font/woff2"


def test_page_additions_are_configured():
    import tomllib
    from config import SCRIPT_DIR
    ui = tomllib.loads((SCRIPT_DIR / ".chainlit" / "config.toml")
                       .read_text(encoding="utf-8"))["UI"]
    assert ui["custom_js"] == "/public/app.js"
    assert ui["custom_css"] == "/public/app.css"
    # Picking an agent in the sidebar opens a new chat without a dialog
    assert ui["confirm_new_chat"] is False
    for f in ("app.js", "app.css", "agents.json", "fonts/SourceSansVF-Upright.woff2",
              "fonts/MaterialSymbolsRounded.woff2"):
        assert (SCRIPT_DIR / "public" / f).is_file(), f


@pytest.mark.parametrize("frame,scalar", [
    (pd.DataFrame({"n": [42]}), True),                              # COUNT(*)
    (pd.DataFrame({"label": ["Sony"], "n": [42]}), True),           # one grouped count
    (pd.DataFrame({"code": ["x"], "type": ["track"], "us": ["No"]}), False),  # a record
    (pd.DataFrame({"n": [1, 2]}), False),
])
def test_only_scalar_results_lose_their_table(frame, scalar):
    """A one-product availability sheet is a record the user asked to
    download; hiding it like a COUNT left them with nothing."""
    assert results.is_scalar_result(frame) is scalar


def test_true_false_columns_are_not_charted():
    import run_state
    import tools
    ctx = run_state.start_run()
    ctx.dataframe = pd.DataFrame({"code": ["a", "b"], "us": [True, False], "t": ["x", "y"]})
    assert tools.visualize_results.func("bar", "code", "us").startswith("Not charted")
    assert tools.visualize_results.func("bar", "code", "t").startswith("Not charted")
    assert ctx.chart is None


def test_app_js_parses():
    """One syntax error stops the whole page script: no landing page, no
    sidebar, no favourites. Checked with Node where it is installed."""
    import shutil
    import subprocess
    from config import SCRIPT_DIR
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run([node, "--check", str(SCRIPT_DIR / "public" / "app.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_hiding_empty_date_groups_cannot_hide_the_whole_sidebar():
    """Chainlit wraps the whole chat list - and our Agents/Favourites sections -
    in an outer sidebar group. An unscoped rule hid it whenever every chat
    was a favourite, blanking the sidebar."""
    from config import SCRIPT_DIR
    js = (SCRIPT_DIR / "public" / "app.js").read_text(encoding="utf-8")
    assert "'#thread-history [data-sidebar=\"group\"]:has(" in js


@pytest.mark.parametrize("value,cap", [(None, 25), ("10", 10), ("0", 0), ("-3", 0), ("x", 25)])
def test_chat_length_cap_setting(monkeypatch, value, cap):
    import chat_store
    if value is None:
        monkeypatch.delenv("CHAT_MAX_QUESTIONS", raising=False)
    else:
        monkeypatch.setenv("CHAT_MAX_QUESTIONS", value)
    assert chat_store.chat_max_questions() == cap


@pytest.mark.parametrize("answered,cap,full", [(24, "25", False), (25, "25", True),
                                                (30, "25", True), (500, "0", False)])
def test_a_chat_is_full_at_its_question_cap(cl_app, monkeypatch, answered, cap, full):
    """Bounds every chat's size; 25 matches MAX_HISTORY, so the model always
    sees the whole chat."""
    from types import SimpleNamespace
    monkeypatch.setenv("CHAT_MAX_QUESTIONS", cap)
    history = [{"question": "q", "answer": "a"}] * answered
    monkeypatch.setattr(cl_app.cl, "user_session", SimpleNamespace(get=lambda k: history))
    assert cl_app._chat_full() is full


def test_a_full_chat_runs_nothing(cl_app):
    src = inspect.getsource(cl_app._answer)
    assert src.index("if _chat_full():") < src.index("start_run(")
    assert "Start a new chat to keep going" in src
    assert cl_app.MAX_HISTORY == 25     # the model sees the whole of a full chat
