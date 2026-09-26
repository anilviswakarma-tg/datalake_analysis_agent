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
        for field in ("key", "icon", "lucide", "label", "desc", "questions"):
            assert a.get(field), f"{a.get('key')} missing {field}"


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
                 "password_auth_callback", "set_chat_profiles"):
        assert getattr(code, hook) is not None, hook


def test_chainlit_run_starts_a_run_context(cl_app):
    """on_message must scope the run with start_run(), never a global."""
    src = inspect.getsource(cl_app.on_message)
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
