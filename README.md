# Datalake Analysis Agent

A natural-language agent over the Tuned Global data lake. Ask a question in
plain English; the agent reads a curated data dictionary, writes Athena SQL,
runs it, and explains the result.

Forked from `DataLakeAgent` on 2026-09-26 to be developed independently.

## Quick start

```bash
cd app
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
./.venv/Scripts/python.exe -m pytest          # ~165 tests, ~40s

# Chainlit UI (the replacement - see ROADMAP phase 2)
./.venv/Scripts/chainlit.exe run chainlit_app.py -w      # http://localhost:8000

# Streamlit UI (being retired)
./.venv/Scripts/streamlit.exe run app.py                 # http://localhost:8501
```

Chainlit needs `CHAINLIT_AUTH_SECRET` in `.env` (generate one with
`./.venv/Scripts/chainlit.exe create-secret`). With `DEV_SKIP_AUTH=true` it
signs you in automatically on a dev machine, as the Streamlit app does.

On macOS/Linux use `.venv/bin/python` instead of `.venv/Scripts/python.exe`.

## Configuration

Copy `app/.env.example` to `app/.env` and fill it in. `.env` is gitignored and
must stay that way — it holds live API keys.

| Setting | Needed for |
|---|---|
| `OPENAI_API_KEY` | the GPT-4o mini model option |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | the Gemini option (free tier: ~20 requests/day, which is roughly 3 questions) |
| `AWS_PROFILE` / `AWS_REGION` | Athena and Glue access |
| `ATHENA_OUTPUT_S3` | where Athena writes results |

Bedrock model options need no key — they authenticate with the AWS IAM role.

## Layout

Modules are listed in dependency order; each may only import ones above it.
`tests/test_wiring.py` enforces this.

| Module | Role |
|---|---|
| `config.py` | env defaults, paths |
| `access.py` | allowed domain, password check, dev-bypass gate |
| `catalogue.py` | the four agents and their suggested questions |
| `aws.py` | boto3 sessions, Athena execution |
| `results.py` | dataframe → chart / CSV / Excel |
| `run_state.py` | per-run result handoff and loop guards |
| `knowledge.py` | reads the data dictionary |
| `entities.py` | label/client name → id resolution |
| `models.py` | model registry, providers, content normalisers |
| `prompt.py` | the system prompt — **all agent behaviour rules** |
| `tools.py` | the agent's tools |
| `agent.py` | wires model + tools + prompt |
| `ui.py`, `auth.py`, `app.py` | Streamlit layer (being retired) |
| `chainlit_app.py` | Chainlit layer — auth callbacks, profiles, the streamed run |

`access.py` (sign-in rules) and `catalogue.py` (the four agents and their
suggested questions) sit at the top of the order and are shared by both UIs.

See [app/ARCHITECTURE.md](app/ARCHITECTURE.md) for detail and
[app/ROADMAP.md](app/ROADMAP.md) for planned work.

## Where knowledge lives

There are exactly two places, and the split is deliberate:

- **What the data means → `app/knowledge/data-dictionary/`.** The single
  authority. A `domain_rules.md` playbook used to sit alongside it and was
  retired on 2026-09-24 because two sources of data truth drifted and
  contradicted each other. Don't start a second one.
- **How the agent behaves → `app/prompt.py`.** Entity-resolution policy,
  reporting format, the never-fabricate rule.

The dictionary is a vendored copy of `reporting-deltalake/docs/data-dictionary`.
See [app/knowledge/README.md](app/knowledge/README.md) before editing it.

## Known issues

- **`AWS_PROFILE` is popped unconditionally** at `app.py:50`, so a local run
  of the Streamlit app can't use a named profile even when `.env` sets one.
  `chainlit_app.py` only pops it inside a container.
- `st.components.v1.html`, used for the session-expiry timer, was slated for
  removal after 2026-06-01. (Streamlit only; Chainlit expires the session
  cookie itself.)

## What was left behind in the fork

- `.github/` — the production EC2 deploy pipeline. Removed deliberately: it
  targets the live instance and S3 bucket, which a side project should not.
- `.venv/`, caches, and the technical-solution `.docx`.
- `DEPLOY.md`, `ec2-setup.sh`, `Dockerfile` and `docker-compose.yml` were kept,
  but the first two describe the original production host.
