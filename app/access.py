"""Who may use the app: the allowed email domain, the shared-password check,
session lifetime, and the container-gated local development bypass.

Framework-free, so the rules are the same whichever UI enforces them."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Optional

ALLOWED_DOMAIN = "tunedglobal.com"
SESSION_TTL_SECONDS = 24 * 60 * 60


def in_container() -> bool:
    """True when running inside Docker.

    This app is ALWAYS containerised when deployed (docker-compose on EC2) and
    NEVER containerised during local development, which makes this a reliable
    "am I on a server" signal. Used to refuse the dev auth bypass even if
    DEV_SKIP_AUTH somehow reaches a deployed .env - e.g. someone copying their
    local .env onto the box.
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


def email_allowed(email: Optional[str]) -> bool:
    return (email or "").strip().lower().endswith(f"@{ALLOWED_DOMAIN}")


def password_login_ok(email: str, password: str) -> bool:
    """The shared-password path: an allowed email plus APP_PASSWORD. An unset
    APP_PASSWORD refuses everyone rather than accepting an empty password."""
    app_password = os.getenv("APP_PASSWORD", "")
    if not app_password or not email_allowed(email):
        return False
    return hmac.compare_digest(password.encode("utf-8"), app_password.encode("utf-8"))


def dev_bypass_requested() -> bool:
    """DEV_SKIP_AUTH=true is set. Anything else leaves normal auth in place."""
    return os.getenv("DEV_SKIP_AUTH", "").strip().lower() == "true"


def dev_bypass_email() -> Optional[str]:
    """The identity to sign in as when the local dev bypass applies, else None.

    Requires DEV_SKIP_AUTH=true AND not running in a container: a deployed
    host carrying the flag is a misconfiguration, and gets normal sign-in.
    """
    if dev_bypass_requested() and not in_container():
        return os.getenv("DEV_USER_EMAIL", f"dev@{ALLOWED_DOMAIN}")
    return None
