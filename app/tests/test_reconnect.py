"""A chat across a dropped connection (chainlit_app, "A CHAT ACROSS
RECONNECTS"): the scan ledger survives the reconnect, and an answer that
finished while the browser was away is shown by reloading the chat.

Fakes stand in for the socket server and the data layer, so these run
offline. The last tests pin the Chainlit behaviour the fix relies on."""
import asyncio
import inspect
from types import SimpleNamespace

import pytest


@pytest.fixture
def cl_app(monkeypatch):
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    import importlib
    import chainlit_app
    app = importlib.reload(chainlit_app)
    monkeypatch.setattr(app, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(app, "RECONNECT_GRACE_SECONDS", 0.1)
    return app


class Browser:
    """One chat's session and its socket, as the app sees them."""

    def __init__(self, cl_app, monkeypatch, steps=("answer-1",)):
        self.connected = {"sid-1"}
        self.reloads = []
        self.alive = True
        self.thread = {"steps": [{"id": s} for s in steps]}

        async def emit(event, data):
            self.reloads.append(self.session.socket_id)
        self.session = SimpleNamespace(id="sess", socket_id="sid-1", thread_id="t1", emit=emit)

        async def get_thread(thread_id):
            return self.thread
        layer = SimpleNamespace(get_thread=get_thread)
        monkeypatch.setattr(cl_app, "get_data_layer", lambda: layer)
        monkeypatch.setattr(cl_app, "_connected", lambda sid: sid in self.connected)
        monkeypatch.setattr(cl_app, "WebsocketSession", SimpleNamespace(
            get_by_id=lambda sid: self.session if self.alive else None))
        monkeypatch.setattr(cl_app.cl, "context", SimpleNamespace(session=self.session))

    def drop(self):
        self.connected.clear()

    def reconnect(self, sid="sid-2"):
        self.session.socket_id = sid      # what Chainlit's session.restore() does
        self.connected = {sid}


def _after_question(cl_app, browser, during=None, after=None, after_delay=0.2,
                    message_id="answer-1"):
    """Run the reload check as on_message does at the end of a question,
    with `during` happening mid-question and `after` `after_delay` seconds
    after it is answered (the grace period is 0.1 s here)."""
    async def go():
        started_on = browser.session.socket_id
        if during:
            during()
        cl_app._reload_if_disconnected(started_on, SimpleNamespace(id=message_id))
        await asyncio.sleep(after_delay)
        if after:
            after()
        for _ in range(50):
            if browser.reloads:
                break
            await asyncio.sleep(0.01)
    asyncio.run(go())
    return browser.reloads


# ── #1 the answer reaches the page ──────────────────────────────────────────

def test_a_steady_connection_is_never_reloaded(cl_app, monkeypatch):
    assert _after_question(cl_app, Browser(cl_app, monkeypatch)) == []


def test_a_reconnect_long_after_the_answer_is_not_reloaded(cl_app, monkeypatch):
    b = Browser(cl_app, monkeypatch)
    assert _after_question(cl_app, b, after=b.reconnect, after_delay=0.3) == []


def test_a_connection_that_died_silently_is_reloaded(cl_app, monkeypatch):
    """What the browser test hit: offline mid-question, but the server only
    notices at the next missed heartbeat, so at the answer the socket still
    looked connected. The reconnect comes within the grace period."""
    b = Browser(cl_app, monkeypatch)
    assert _after_question(cl_app, b, after=b.reconnect, after_delay=0.03) == ["sid-2"]


def test_back_before_the_answer_finished_reloads_straight_away(cl_app, monkeypatch):
    b = Browser(cl_app, monkeypatch)
    assert _after_question(cl_app, b, during=lambda: (b.drop(), b.reconnect())) == ["sid-2"]


def test_still_away_when_the_answer_finished_reloads_on_return(cl_app, monkeypatch):
    b = Browser(cl_app, monkeypatch)
    assert _after_question(cl_app, b, during=b.drop, after=b.reconnect) == ["sid-2"]


def test_never_back_means_no_reload(cl_app, monkeypatch):
    b = Browser(cl_app, monkeypatch)

    def session_expires():
        b.alive = False                   # Chainlit's session_timeout passed
    assert _after_question(cl_app, b, during=b.drop, after=session_expires) == []


def test_without_chat_history_there_is_nothing_to_reload(cl_app, monkeypatch):
    b = Browser(cl_app, monkeypatch)
    monkeypatch.setattr(cl_app, "get_data_layer", lambda: None)
    assert _after_question(cl_app, b, during=lambda: (b.drop(), b.reconnect())) == []


def test_the_reload_waits_for_the_answer_to_be_saved(cl_app, monkeypatch):
    """Chainlit writes messages in background tasks; reloading before the
    answer is in the database would reopen the chat without it."""
    b = Browser(cl_app, monkeypatch, steps=())
    seen = []

    def saved_later():
        seen.append(list(b.reloads))      # not reloaded while unsaved
        b.thread["steps"].append({"id": "answer-1"})
    _after_question(cl_app, b, during=lambda: (b.drop(), b.reconnect()), after=saved_later)
    assert seen == [[]] and b.reloads == ["sid-2"]


def test_every_question_ends_with_the_reload_check(cl_app):
    src = inspect.getsource(cl_app.on_message)
    assert "socket_id = cl.context.session.socket_id" in src
    assert "_reload_if_disconnected(socket_id, sent)" in src.split("finally:")[1]


# ── #3 the scan ledger survives ─────────────────────────────────────────────

def test_the_ledger_belongs_to_the_chat_not_the_session(cl_app, monkeypatch):
    """A reconnect replaces cl.user_session and a reload starts a new
    session; neither may reset the chat's scan budget."""
    first = SimpleNamespace(thread_id="t1")
    monkeypatch.setattr(cl_app.cl, "context", SimpleNamespace(session=first))
    ledger = cl_app._ledger()
    ledger.bytes_scanned = 40 * 1024 ** 3
    monkeypatch.setattr(cl_app.cl, "context",
                        SimpleNamespace(session=SimpleNamespace(thread_id="t1")))
    assert cl_app._ledger() is ledger
    monkeypatch.setattr(cl_app.cl, "context",
                        SimpleNamespace(session=SimpleNamespace(thread_id="t2")))
    assert cl_app._ledger() is not ledger


def test_old_ledgers_are_dropped_first(cl_app, monkeypatch):
    monkeypatch.setattr(cl_app, "_MAX_LEDGERS", 2)
    for t in ("a", "b", "a", "c"):        # "a" used again, so "b" is oldest
        monkeypatch.setattr(cl_app.cl, "context",
                            SimpleNamespace(session=SimpleNamespace(thread_id=t)))
        cl_app._ledger()
    assert list(cl_app._LEDGERS) == ["a", "c"]


def test_nothing_keeps_the_ledger_in_the_user_session(cl_app):
    src = inspect.getsource(cl_app)
    assert 'user_session.set("ledger"' not in src and 'user_session.get("ledger"' not in src


# ── what Chainlit does, which the fix relies on ─────────────────────────────

def test_chainlit_still_replaces_the_user_session_on_resume():
    """If this changes, the ledger could move back into cl.user_session."""
    import chainlit.socket as cl_socket
    assert "user_sessions[session.id] = metadata.copy()" in inspect.getsource(cl_socket.resume_thread)


def test_chainlit_reconnect_moves_the_session_to_a_new_socket():
    from chainlit.session import WebsocketSession
    src = inspect.getsource(WebsocketSession.restore)
    assert "self.socket_id = new_socket_id" in src


def test_chainlit_frontend_still_reloads_on_request():
    from test_chainlit_app import _frontend_bundle
    bundle = _frontend_bundle()
    assert 'on("reload",()=>{' in bundle and "window.location.reload()" in bundle


def test_socketio_can_still_tell_if_a_socket_is_connected(cl_app):
    assert cl_app._connected("no-such-socket") is False


def test_the_grace_period_covers_a_missed_heartbeat():
    import importlib
    import chainlit_app
    app = importlib.reload(chainlit_app)
    eio = app.cl_server.sio.eio
    assert app.RECONNECT_GRACE_SECONDS > eio.ping_interval + eio.ping_timeout
