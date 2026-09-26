"""Access control: the login screen, Google SSO and shared-password paths,
session expiry, and the container-gated local development bypass."""

from __future__ import annotations

import os
import time
from pathlib import Path

import streamlit as st

from ui import _inject_css, _logo_data_uri



# ═══════════════════════════════════════════════════════════════════════════
# 11. STREAMLIT UI
# ═══════════════════════════════════════════════════════════════════════════

_ALLOWED_DOMAIN = "tunedglobal.com"


def _in_container() -> bool:
    """True when running inside Docker.

    This app is ALWAYS containerised when deployed (docker-compose on EC2) and
    NEVER containerised during local development (bare `streamlit run`), which
    makes this a reliable "am I on a server" signal. Used to refuse the dev
    auth bypass even if DEV_SKIP_AUTH somehow reaches a deployed .env — e.g.
    someone copying their local .env onto the box.
    """
    try:
        if Path("/.dockerenv").exists():
            return True
    except OSError:
        pass
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False


def _auth_configured() -> bool:
    """True when Google OIDC login is configured in .streamlit/secrets.toml.

    Requires an [auth] section with a provider sub-table containing a
    client_id, so a half-filled secrets file doesn't flip the app into Google
    mode (and lock out the password fallback) before it's actually ready.
    """
    try:
        if "auth" not in st.secrets:
            return False
        auth = st.secrets["auth"]
    except Exception:
        return False
    for value in getattr(auth, "values", lambda: [])():
        try:
            if value.get("client_id"):
                return True
        except AttributeError:
            continue
    return False


def _check_auth() -> None:
    """Block access unless the user has logged in with a @tunedglobal.com email
    and the correct APP_PASSWORD from .env.

    Stores ``st.session_state.authenticated`` (bool) and
    ``st.session_state.user_email`` (str) on success.
    """
    # ── LOCAL DEV ONLY: skip the login screen ────────────────────────────
    # Enabled by DEV_SKIP_AUTH=true, which lives in .env. That file is
    # excluded from BOTH the release tarball (.github/workflows/deploy.yml)
    # and the extraction on the box (.github/deploy/remote_deploy.sh), so it
    # cannot reach prod through the pipeline. Defaults OFF: anything other
    # than an explicit "true" leaves normal auth in place. The banner is
    # deliberately loud and on every page — if you ever see it on a shared
    # host, that host's .env is wrong.
    if os.getenv("DEV_SKIP_AUTH", "").strip().lower() == "true":
        if _in_container():
            # Second gate: the flag is set, but we're containerised, so this is
            # a deployed environment. Ignore the flag and fall through to the
            # real login screen — but say so loudly, because a server .env
            # carrying this flag is a misconfiguration someone must fix.
            st.error(
                "⚠️ `DEV_SKIP_AUTH` is set on a containerised (deployed) host. "
                "Ignoring it and requiring normal sign-in. Remove this flag "
                "from the server's `.env`."
            )
        else:
            st.session_state.authenticated = True
            st.session_state.user_email = os.getenv("DEV_USER_EMAIL", "dev@tunedglobal.com")
            st.session_state.auth_method = "dev-bypass"
            st.session_state.login_time = time.time()
            st.warning(
                "🔓 **Auth bypassed** — `DEV_SKIP_AUTH=true` is set. "
                "Local development only; unset it in `.env` to restore the login screen."
            )
            return

    _SESSION_TTL = 24 * 60 * 60  # 24 hours in seconds
    if st.session_state.get("authenticated"):
        login_time = st.session_state.get("login_time", 0)
        if time.time() - login_time > _SESSION_TTL:
            # Session expired — clear auth state and fall through to login screen
            for key in ("authenticated", "user_email", "auth_method", "login_time"):
                st.session_state.pop(key, None)
            st.info("Your session has expired. Please sign in again.")
        else:
            return  # valid session — nothing to do

    # Accept an existing, valid Google SSO session before drawing any login UI
    # (so the happy path never flashes the login screen).
    if _auth_configured() and st.user.is_logged_in and \
            (st.user.email or "").lower().endswith(f"@{_ALLOWED_DOMAIN}"):
        st.session_state.authenticated = True
        st.session_state.user_email = (st.user.email or "").lower()
        st.session_state.auth_method = "google"
        st.session_state.login_time = time.time()
        return

    app_password = os.getenv("APP_PASSWORD", "")

    # ── Sandbox-style dark login screen (matches apis-playground) ────────────
    logo = _logo_data_uri()
    logo_html = (
        f'<img src="{logo}" alt="Tuned Global" />' if logo
        else '<span style="color:#E85420;font-size:26px;font-weight:700;">Tuned Global</span>'
    )
    _inject_css("login.css")
    st.markdown(f'<div class="tg-logo">{logo_html}</div>', unsafe_allow_html=True)

    with st.container(border=True, key="login_card"):
        intro = (
            '<div class="tg-badge">DATA LAKE AGENT</div>'
            '<div class="tg-title">Welcome to the<br>Data Lake Agent</div>'
            '<div class="tg-desc">Ask questions in plain English across the catalogue, '
            'playlog and store domains — the agent writes and runs the Athena SQL, then '
            'shows you the results.</div>'
        )
        if _auth_configured():
            # ── Google Workspace SSO (preferred) ─────────────────────────
            st.markdown(intro, unsafe_allow_html=True)
            if st.user.is_logged_in:
                # Authenticated with Google, but outside the allowed domain.
                st.error(
                    f"Only @{_ALLOWED_DOMAIN} accounts are allowed. "
                    f"You're signed in as {st.user.email}."
                )
                if st.button("Sign out and try another account",
                             use_container_width=True, type="primary"):
                    st.logout()
            else:
                if st.button("Continue with Google  →",
                             use_container_width=True, type="primary"):
                    st.login("google")
        else:
            # ── Shared-password fallback (until Google OAuth is configured) ──
            st.markdown(intro, unsafe_allow_html=True)
            email = st.text_input(
                "Email",
                placeholder="yourname@tunedglobal.com",
                key="_login_email",
                label_visibility="visible",
            )
            password = st.text_input(
                "Password",
                type="password",
                key="_login_password",
            )
            if st.button("Sign In  →", use_container_width=True, type="primary"):
                if not email.lower().endswith(f"@{_ALLOWED_DOMAIN}"):
                    st.error(f"Access is restricted to @{_ALLOWED_DOMAIN} accounts.")
                elif not app_password:
                    st.error("APP_PASSWORD is not set on the server — contact your admin.")
                elif password != app_password:
                    st.error("Incorrect password. Please try again.")
                else:
                    st.session_state.authenticated = True
                    st.session_state.user_email = email.lower()
                    st.session_state.auth_method = "password"
                    st.session_state.login_time = time.time()
                    st.rerun()

    # Footer note (below the card, sandbox-style)
    st.markdown(
        '<div class="tg-footer">Access is restricted to @tunedglobal.com accounts.'
        '<br>Need access? <a href="mailto:it@tunedglobal.com">Contact your admin →</a></div>',
        unsafe_allow_html=True,
    )

    st.stop()
