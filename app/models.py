"""Chat model construction: the selectable model registry, Bedrock Mantle
SigV4 auth, and the active choice shared with the tools."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from langchain.chat_models import init_chat_model
from langchain_aws import ChatBedrockConverse
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from aws import _session
from run_state import DEFAULT_MODEL_CHOICE, active_run



class _MantleAuth(httpx.Auth):
    """Signs every request with AWS SigV4 so the EC2 IAM role works with Mantle."""

    def __init__(self, region: str = "us-west-2"):
        self._region = region

    def auth_flow(self, request: httpx.Request):
        # Resolve credentials the same way every other AWS call in this app does
        # (AWS_PROFILE locally, the instance IAM role on EC2). A bare
        # boto3.Session() here signed as whatever the *default* profile happened
        # to be — on a dev machine that is a different account from the one
        # holding the data, so Mantle 403'd while Athena queries succeeded.
        creds = _session().get_credentials().get_frozen_credentials()
        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(creds, "bedrock", self._region).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        yield request


def _mantle_client(model_id: str, region: str = "us-west-2", path: str = "v1") -> "ChatOpenAI":
    """Return a ChatOpenAI pointed at the Bedrock Mantle endpoint with IAM auth."""
    return ChatOpenAI(
        model=model_id,
        base_url=f"https://bedrock-mantle.{region}.api.aws/{path}",
        api_key="not-used",          # SigV4 handles auth — key field must be non-empty
        http_client=httpx.Client(auth=_MantleAuth(region), timeout=120),
        temperature=0.0,
    )


# ── Model registry — add/change entries here to extend the selector ──────────
#
# Providers:
#   openai             the OpenAI API proper, keyed by OPENAI_API_KEY
#   bedrock_mantle     Bedrock Mantle over SigV4 — no key, uses the IAM role
#   bedrock_converse   the standard Bedrock Converse API (no entry yet)
#   google_genai       the native Google SDK (see the gemini entry for why)
#   openai_compatible  any vendor exposing an OpenAI-shaped /chat endpoint —
#                      the same trick _mantle_client uses. Adding Groq,
#                      Cerebras, OpenRouter or a local Ollama is one entry
#                      here: base_url, model_id and the env var holding its
#                      key. Do NOT use it for a model that needs to round-trip
#                      provider-specific fields on tool calls; Gemini's
#                      thought_signature is exactly that case.
_BEDROCK_REGION = "us-west-2"


def _pretty_model_name(model_id: str) -> str:
    """'gemini-3.6-flash' -> 'Gemini 3.6 Flash'.

    The dropdown label is derived from the model id rather than typed out, so
    bumping GEMINI_MODEL in .env can't leave the UI advertising the old
    version. Vendors rename these often enough for that to matter.
    """
    return " ".join(
        part.capitalize() if part[:1].isalpha() else part
        for part in model_id.split("-")
    )


# Overridable without a code change — vendors rename models frequently.
_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

_MODEL_REGISTRY: dict = {
    "gpt4o-mini": {
        "label": "GPT-4o mini (OpenAI)",
        "provider": "openai",
        # Declared so the generic key check covers this entry too — without it
        # a placeholder OPENAI_API_KEY still let the model be recommended as a
        # working alternative.
        "api_key_env": "OPENAI_API_KEY",
    },
    "deepseek": {
        "label": "DeepSeek V3.2 (Bedrock)",
        "provider": "bedrock_mantle",
        "model_id": "deepseek.v3.2",
        "mantle_path": "v1",
    },
    "grok": {
        "label": "Grok 4.3 (Bedrock)",
        "provider": "bedrock_mantle",
        "model_id": "xai.grok-4.3",
        "mantle_path": "openai/v1",
    },
    "glm": {
        "label": "GLM-5 (Bedrock)",
        "provider": "bedrock_mantle",
        "model_id": "zai.glm-5",
        "mantle_path": "v1",
    },
    # Native SDK, NOT Google's OpenAI-compatible endpoint. Gemini 3.x attaches
    # a `thought_signature` to every function call and requires it back on the
    # next turn; the OpenAI wire format has no field for it, so ChatOpenAI drops
    # it and the second tool call fails with "Function call is missing a
    # thought_signature". Verified: the compat endpoint fails on a two-step tool
    # round-trip even with reasoning_effort="none"; the native client passes.
    "gemini": {
        "label": f"{_pretty_model_name(_GEMINI_MODEL)} (Google)",
        "provider": "google_genai",
        "model_id": _GEMINI_MODEL,
        "api_key_env": "GEMINI_API_KEY",
        # Runs on the vendor's free tier — no spend, subject to rate limits.
        # Bedrock and OpenAI entries are billed (to the AWS account and per
        # token respectively), so neither is marked free.
        "free": True,
    },
}

FREE_PREFIX = "FREE · "


def model_label(model_choice: str) -> str:
    """The dropdown label, prefixed when the model costs nothing to run.

    Derived from the `free` flag rather than baked into `label`, so a new free
    entry only sets `"free": True` and the prefix can never drift from the name.
    """
    cfg = _MODEL_REGISTRY.get(model_choice)
    if cfg is None:
        return model_choice
    return f"{FREE_PREFIX}{cfg['label']}" if cfg.get("free") else cfg["label"]


def _placeholder_key(value: str) -> bool:
    """True when a key is absent or still the .env.example placeholder."""
    v = (value or "").strip()
    return not v or "REPLACE" in v.upper() or v in {"sk-...", "..."}


def _require_key(cfg: dict) -> str:
    """The vendor key for this entry, or raise.

    Raises rather than letting the vendor return a 401 that reads like an app
    bug. app.py calls missing_key_reason() first, so reaching here means a
    non-UI caller (a tool, a script) built the model.
    """
    key = os.getenv(cfg["api_key_env"], "").strip()
    if _placeholder_key(key):
        raise ValueError(
            f"{cfg['api_key_env']} is not set to a usable key "
            f"(needed for {cfg['label']})."
        )
    return key


def explain_failure(exc: Exception) -> str | None:
    """Turn a known provider failure into an actionable message, else None.

    Without this the UI tells end users "something went wrong, please try
    again" — actively wrong advice for a quota error, where retrying cannot
    help until the window resets, and for an auth error, where it never will.
    """
    msg = str(exc)
    low = msg.lower()
    choice = active_model_choice()
    cfg = _MODEL_REGISTRY.get(choice) or {}
    name = cfg.get("label", choice)
    # Only suggest alternatives that are actually configured — offering a model
    # whose own key is missing just sends the user round the same loop. A
    # Bedrock entry's IAM permissions can't be checked without calling it, so
    # those are always listed.
    others = ", ".join(
        model_label(k) for k in _MODEL_REGISTRY
        if k != choice and missing_key_reason(k) is None
    ) or "none are currently configured"

    if "resource_exhausted" in low or "429" in msg or "quota" in low or "rate limit" in low:
        cap = ""
        if m := re.search(r"limit:\s*(\d+)", msg):
            cap = f" The cap is {m.group(1)} requests."
        per_day = "perday" in low.replace("_", "").replace("-", "")
        when = "tomorrow" if per_day else "in a minute"
        return (
            f"**{name} has hit its request quota.**{cap} One question uses "
            f"several requests, so a free tier is spent quickly. Wait until "
            f"{when}, or pick another model from the dropdown ({others})."
        )
    if "401" in msg or "invalid_api_key" in low or "incorrect api key" in low:
        env = cfg.get("api_key_env", "the API key")
        return (
            f"**{name} rejected the API key.** Set a valid `{env}` in `.env` "
            "and restart the app, or pick another model from the dropdown."
        )
    if "403" in msg or "accessdenied" in low or "not authorized" in low:
        return (
            f"**Not authorised to call {name}.** The AWS credentials in use "
            "lack permission for this model. Pick another model from the "
            f"dropdown ({others}), or ask for the permission to be granted."
        )
    return None


def missing_key_reason(model_choice: str) -> str | None:
    """A user-facing message when the chosen model can't authenticate, else None.

    Lets app.py fail fast with a readable error instead of letting a missing or
    placeholder key surface as a raw 401 from inside the agent run. Bedrock
    choices need no key (SigV4 / IAM role), so they always return None.
    """
    cfg = _MODEL_REGISTRY.get(model_choice)
    if cfg is None:
        return f"Unknown model {model_choice!r}."
    # Any entry naming a key env var is checked, whatever its provider.
    if env := cfg.get("api_key_env"):
        if _placeholder_key(os.getenv(env, "")):
            return (
                f"{cfg['label']} needs a real `{env}` in `.env` "
                "(the placeholder value doesn't work), then restart the app."
            )
    return None


def active_model_choice() -> str:
    """The model selected for the current run, so tools that spin up their own
    LLM (e.g. sql_db_query_checker) honour the user's choice instead of always
    defaulting to OpenAI. Read from the run context, not a module global, so
    two users on different models can't swap each other's choice."""
    run = active_run()
    return run.model_choice if run else DEFAULT_MODEL_CHOICE


def build_active_llm():
    """A chat model for the currently-selected choice."""
    return _build_llm(active_model_choice())


def _build_llm(model_choice: str):
    """Build a chat model from a _MODEL_REGISTRY choice. Shared by the main
    agent and by tools (like the SQL checker) so every LLM call uses the model
    the user actually selected — not a hardcoded OpenAI default."""
    # Fail loudly on an unrecognised choice. This used to fall back to the
    # OpenAI entry, which meant a stale or misspelled selection quietly hit
    # api.openai.com and surfaced as "401 Incorrect API key provided:
    # sk-REPLACE_ME" — an OpenAI error for a run that never intended to use
    # OpenAI. app.py only key-checks choices whose provider is "openai", so the
    # fallback also slipped past that guard.
    cfg = _MODEL_REGISTRY.get(model_choice)
    if cfg is None:
        raise ValueError(
            f"Unknown model choice {model_choice!r}. "
            f"Valid choices: {', '.join(_MODEL_REGISTRY)}."
        )
    provider = cfg["provider"]

    if provider == "bedrock_converse":
        return ChatBedrockConverse(
            model=cfg["model_id"],
            region_name=_BEDROCK_REGION,
            temperature=0.0,
        )
    if provider == "bedrock_mantle":
        return _mantle_client(cfg["model_id"], region=_BEDROCK_REGION, path=cfg.get("mantle_path", "v1"))
    if provider == "google_genai":
        # temperature is deliberately not passed: Gemini 3.x flash uses fixed
        # sampling defaults and warns that the parameter is ignored.
        return ChatGoogleGenerativeAI(
            model=cfg["model_id"],
            google_api_key=_require_key(cfg),
        )
    if provider == "openai_compatible":
        key = _require_key(cfg)
        return ChatOpenAI(
            model=cfg["model_id"],
            base_url=cfg["base_url"],
            api_key=key,
            temperature=0.0,
            timeout=120,
        )
    # openai
    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    seed = int(os.getenv("OPENAI_SEED", "42"))
    return init_chat_model(model_name, temperature=0, timeout=60, model_kwargs={"seed": seed})


# ── Message content normalisation ───────────────────────────────────────────
# Providers disagree on the shape of a message's `.content`. OpenAI and Bedrock
# return a plain string; the native Google client returns a list of typed
# blocks, e.g. [{"type": "text", "text": "..."}]. Passing that list to
# st.markdown() renders a raw Python repr to the user, so every read of
# `.content` destined for display goes through a normaliser first.


class ContentNormalizer(ABC):
    """Flattens one provider's message content to displayable text.

    `to_text` handles the shapes common to every provider; subclasses only say
    how to read a single block, which is the part that actually differs.
    """

    def to_text(self, content) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(self._block_text(b) for b in content)
        return str(content)

    @abstractmethod
    def _block_text(self, block) -> str:
        """The displayable text in one block, or "" if it carries none."""


class PassthroughNormalizer(ContentNormalizer):
    """The default: providers whose content is already a plain string.

    OpenAI and Bedrock never emit typed blocks, so `to_text` returns their
    string untouched and this method is effectively unreachable. It stays
    lenient rather than raising, so an unexpected shape degrades to dropped
    text rather than a crash mid-render.
    """

    def _block_text(self, block) -> str:
        return block if isinstance(block, str) else ""


class GeminiNormalizer(ContentNormalizer):
    """The native Google client, which returns a list of typed blocks.

    Only "text" blocks are shown. Thinking, tool_use and inline-image blocks
    carry nothing meaningful for the answer pane and would otherwise be
    repr'd into it.
    """

    TEXT_BLOCK_TYPES = frozenset({"text"})

    def _block_text(self, block) -> str:
        if isinstance(block, str):
            return block
        if isinstance(block, dict) and block.get("type") in self.TEXT_BLOCK_TYPES:
            return block.get("text") or ""
        return ""


# Providers absent from this map need no normalisation and get the default.
_NORMALIZERS: dict[str, ContentNormalizer] = {
    "google_genai": GeminiNormalizer(),
}
_DEFAULT_NORMALIZER = PassthroughNormalizer()


def normalizer_for(provider: str | None) -> ContentNormalizer:
    """The normaliser for a provider name, falling back to the default."""
    return _NORMALIZERS.get(provider or "", _DEFAULT_NORMALIZER)


def active_normalizer() -> ContentNormalizer:
    """The normaliser for the model currently selected in the sidebar."""
    cfg = _MODEL_REGISTRY.get(active_model_choice()) or {}
    return normalizer_for(cfg.get("provider"))


def message_text(content) -> str:
    """Flatten message content for display, using the active model's rules."""
    return active_normalizer().to_text(content)
