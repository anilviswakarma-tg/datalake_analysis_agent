"""The local-development auth bypass must never fire on a deployed host.

Two gates: the flag must be exactly "true", and the process must not be
running in a container. These tests exist because the failure mode is silent
and severe (an internal tool served with no authentication at all).
"""
import pytest

import auth


class _LoginScreenShown(Exception):
    """Raised by the stub whenever the real login screen would be rendered."""


class _State(dict):
    def __setattr__(self, k, v):
        self[k] = v

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


class _FakeStreamlit:
    """Anything on the login-screen path raises, so 'the bypass did not fire'
    is unambiguous rather than merely producing no output."""

    _LOGIN_PATH = ("markdown", "container", "button", "text_input",
                   "stop", "rerun", "login", "logout", "secrets", "user")

    def __init__(self):
        self.session_state = _State()
        self.warnings = []
        self.errors = []

    def __getattr__(self, name):
        if name in self._LOGIN_PATH:
            def _raise(*a, **k):
                raise _LoginScreenShown(f"st.{name}")
            return _raise
        raise AttributeError(name)

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)

    def info(self, msg):
        pass


def _run(monkeypatch, flag, in_container):
    """Call _check_auth under the given conditions.

    Returns (bypassed, fake_streamlit).
    """
    if flag is None:
        monkeypatch.delenv("DEV_SKIP_AUTH", raising=False)
    else:
        monkeypatch.setenv("DEV_SKIP_AUTH", flag)
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@tunedglobal.com")

    fake = _FakeStreamlit()
    monkeypatch.setattr(auth, "st", fake)
    monkeypatch.setattr(auth, "_in_container", lambda: in_container)
    try:
        auth._check_auth()
        return True, fake
    except _LoginScreenShown:
        return False, fake


TRUTHY = ["true", "TRUE", "  true  "]
# Everything else must leave authentication switched on.
NOT_TRUTHY = [None, "", "false", "0", "1", "yes", "y", "True ish", "TRUEX", "none"]


@pytest.mark.parametrize("flag", TRUTHY)
def test_bypass_works_locally(monkeypatch, flag):
    bypassed, fake = _run(monkeypatch, flag, in_container=False)
    assert bypassed
    assert fake.session_state.get("authenticated") is True
    assert fake.session_state.get("auth_method") == "dev-bypass"
    assert fake.session_state.get("user_email") == "dev@tunedglobal.com"
    # The banner is the thing that stops it being left on by accident.
    assert len(fake.warnings) == 1 and "Auth bypassed" in fake.warnings[0]


@pytest.mark.parametrize("flag", TRUTHY)
def test_bypass_refused_in_a_container(monkeypatch, flag):
    """Deployed hosts are containerised. Even with the flag set, refuse."""
    bypassed, fake = _run(monkeypatch, flag, in_container=True)
    assert not bypassed, "bypass fired on a containerised host"
    assert fake.session_state.get("authenticated") is not True
    assert len(fake.errors) == 1 and "containerised" in fake.errors[0]


@pytest.mark.parametrize("flag", NOT_TRUTHY)
@pytest.mark.parametrize("in_container", [False, True])
def test_bypass_stays_off_for_anything_else(monkeypatch, flag, in_container):
    bypassed, _ = _run(monkeypatch, flag, in_container)
    assert not bypassed


def test_container_detector_is_false_on_a_dev_machine():
    assert auth._in_container() is False
