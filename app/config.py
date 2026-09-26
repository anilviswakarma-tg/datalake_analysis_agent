"""Configuration: environment defaults, filesystem paths, and the small
accessors that read them. Imports nothing else in this package."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()



# ═══════════════════════════════════════════════════════════════════════════
# 0. CONFIG
# ═══════════════════════════════════════════════════════════════════════════

DEFAULTS = {
    # NOTE: AWS_PROFILE intentionally NOT set here. On EC2/Docker, leave it
    # unset so boto3 uses the attached IAM instance role automatically.
    # Setting it to any non-empty value (even a real account/profile name)
    # breaks credential resolution inside the container, since there's no
    # ~/.aws/config defining that profile there. Only set AWS_PROFILE via
    # .env / the sidebar field for LOCAL development with `aws sso login`.
    "AWS_REGION":        "us-west-2",
    "ATHENA_BRONZE_DB":  "tg-deltalake-bronze",
    "ATHENA_SILVER_DB":  "tg-deltalake-silver",
    "ATHENA_MASTER_DB":  "tg-master",
    "ATHENA_OUTPUT_S3":  "s3://athena-results-223829094007-us-west-2/",
    "ATHENA_WORKGROUP":  "primary",
    "OPENAI_MODEL":      "gpt-4o",
    # Fixed seed for OpenAI's best-effort reproducible-output feature. Doesn't
    # guarantee byte-identical completions on its own, but combined with
    # temperature=0 it measurably reduces run-to-run variance. Change this
    # value only if you intentionally want a different (but still fixed)
    # sampling path.
    "OPENAI_SEED":       "42",
}
for key, value in DEFAULTS.items():
    os.environ.setdefault(key, value)

VALID_DOMAINS = {"catalogue", "playlog", "store"}

# Knowledge files (relative to this script)
SCRIPT_DIR = Path(__file__).parent.resolve()
KNOWLEDGE_DIR = SCRIPT_DIR / "knowledge"
FEEDBACK_FILE = KNOWLEDGE_DIR / "feedback.md"
# Vendored copy of reporting-deltalake/docs/data-dictionary — the authoritative
# per-table reference, maintained by the data team in that repo. Refresh it by
# re-copying the folder; don't hand-edit these files here or the two diverge.
DATA_DICT_DIR = KNOWLEDGE_DIR / "data-dictionary"
DATA_DICT_README = DATA_DICT_DIR / "README.md"
LOGO_FILE = SCRIPT_DIR / "assets" / "logo.png"


def _openai_key_looks_real() -> bool:
    """True when OPENAI_API_KEY is set to something that could actually work.

    Treats the .env/.env.example placeholders as "not set" so the app fails
    fast with a clear message instead of surfacing a 401 mid-run.
    """
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or not key.startswith("sk-"):
        return False
    return "REPLACE" not in key.upper() and key != "sk-..."


def _output_s3() -> str:
    return os.getenv("ATHENA_OUTPUT_S3", "")


def _workgroup() -> str:
    return os.getenv("ATHENA_WORKGROUP", "primary")


def _bronze_db() -> str:
    return os.getenv("ATHENA_BRONZE_DB", "tg-deltalake-bronze")


def _silver_db() -> str:
    return os.getenv("ATHENA_SILVER_DB", "tg-deltalake-silver")


def _master_db() -> str:
    return os.getenv("ATHENA_MASTER_DB", "tg-master")


def _known_databases() -> List[str]:
    return [_bronze_db(), _silver_db(), _master_db()]
