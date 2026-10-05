# Architecture

An app that turns plain-English questions into AWS Athena SQL, runs them, and
shows the results. Two UIs share everything below them: `chainlit_app.py`
(`chainlit run chainlit_app.py`), which is replacing `app.py`
(`streamlit run app.py`).

## Modules

Listed in dependency order. **A module may only import ones above it** — that
rule is what keeps the graph acyclic, and `tests/test_wiring.py` enforces it.

| Module | Lines | Responsibility |
|---|---|---|
| `config.py` | ~90 | Environment defaults, filesystem paths, database-name accessors. Imports nothing else here. |
| `access.py` | ~65 | Sign-in rules: allowed domain, shared password, session TTL, container-gated dev bypass. |
| `catalogue.py` | ~85 | The four landing-page agents and their suggested questions. Writes `public/agents.json` for the Chainlit page (`python catalogue.py`). |
| `aws.py` | ~200 | boto3 sessions and clients, credential retry, the Athena query executor (bytes scanned per query; stops a timed-out query), S3 result fetch. The only module that talks to AWS. |
| `results.py` | ~80 | Chart selection, CSV/Excel export bytes. Framework-free; rendering is in the UI layer. |
| `run_state.py` | ~200 | The per-run context (inputs + results) handed from agent tools to the UI, the loop guards, and `tracked_query`, which every Athena call goes through so its scan is recorded. |
| `knowledge.py` | ~170 | Reading the data dictionary; appending the agent's findings to `feedback.md`. |
| `entities.py` | ~90 | Resolving names ("Sony", "Etisalat") to `owner_id` / `group_id`. |
| `models.py` | ~385 | Model registry, Bedrock Mantle SigV4 auth, content normalisers. |
| `prompt.py` | ~165 | The agent system prompt. |
| `tools.py` | ~510 | The 13 tools the agent can call. |
| `agent.py` | ~40 | Assembles model + tools + prompt into the LangChain agent. |
| `chat_store.py` | ~290 | Chat history: which database (`CHAT_DB_URL`), the portable schema, the result snapshot saved with each answer, favourite chats and the retention sweep (`CHAT_RETENTION_DAYS`, default 60), Athena usage per query (`query_usage`) and user tiers (`user_tiers`). No UI imports. |
| `ui.py` | ~205 | Styling, the agent catalogue, conversation buckets, rendering a run. |
| `auth.py` | ~195 | Login screen, SSO/password paths, session expiry, dev bypass. |
| `app.py` | ~360 | Streamlit page composition and run loop. Being retired. |
| `chainlit_data.py` | ~75 | Chainlit's SQLAlchemy data layer, fixed to work on any backend. |
| `chainlit_app.py` | ~440 | Chainlit auth callbacks, model picker, settings, streamed run, chat resume. Entry point. |

**Browser layer (`public/`).** `app.js` and `app.css` redraw the Streamlit
layout inside Chainlit's page: the hero and agent tiles, per-agent question
tiles, the sidebar logo / AGENTS list / signed-in footer, the title bar, and
the chart → table → downloads order in answers. They hook onto Chainlit's
element ids, not its utility classes; `tests/test_chainlit_app.py` checks
those ids still exist in the bundled frontend, so re-run it after any
Chainlit upgrade. Fonts (Source Sans, Material Symbols) are self-hosted
copies of the ones Streamlit ships.

### Why the boundaries fall where they do

- **`run_state.py` is separate** because agent tools execute inside the agent
  loop with no access to the UI's call stack. The UI calls `start_run()` with
  the question, model and live flag; tools read those and deposit results on
  the returned `RunContext`. It lives in a `ContextVar`, never a module global
  or `os.environ`, so concurrent users can't overwrite each other.
  `tests/test_run_state.py` proves tool writes survive LangGraph's worker
  threads, sync and async.
- **`models.py` is split from `agent.py`** to break a cycle: `agent` needs the
  tool list, and `tools.sql_db_query_checker` needs to build an LLM with the
  user's selected model. Both depend on `models`, and neither on the other.
  The choice is read from the run context via `active_model_choice()` /
  `build_active_llm()`, so the agent and the checker can't disagree.
- **`config.py` imports nothing** from this package, so every other module can
  depend on it freely.

### Chat history is database-agnostic

- **The database is one setting.** `CHAT_DB_URL` is a SQLAlchemy async URL.
  Unset means SQLite at `data/chat_history.db` (a Docker volume in
  deployment); `off` disables history. Postgres later is a URL change plus
  `pip install asyncpg`, with no code change.
- **We own the schema** (`chat_store.SCHEMA`, created at startup): portable
  types only, so the same tables work everywhere. Chainlit's published DDL is
  Postgres-only and misses a column it writes.
- **`chainlit_data.py` fixes the stock data layer**, which silently lost
  user messages and thread metadata on SQLite, and logs failed writes as
  errors instead of warnings.
- **No blob storage.** Each answer saves its ~100 preview rows, chart spec and
  query id in its metadata (`chat_store.answer_metadata`). Reopening a chat
  rebuilds the table and chart from that, and re-fetches the full CSV from S3
  by query id while Athena's result object still exists.

## Knowledge sources

Precedence when they disagree — enforced in `prompt.py` and documented in
`knowledge/README.md`:

1. **`knowledge/data-dictionary/`** — authoritative on what tables and columns
   *mean*. Vendored from `reporting-deltalake/docs/data-dictionary`; maintained
   there, not here. Read via `get_data_dictionary(tables)`.
2. **AWS Glue** — authoritative on what *exists* and its type, via
   `describe_table`.

Agent behaviour (date defaults, reporting rules) lives in `prompt.py`. The
`domain_rules.md` playbook that used to sit between these was retired on
2026-09-24; `knowledge/dictionary_gaps.md` is the historical record of that
migration.

**`knowledge/feedback.md`** is not a source: the agent appends observations
there (`capture_finding`) and never reads them. A person reviews them and
promotes the useful ones into the dictionary or `prompt.py`.

## The agent's tools

| Group | Tools |
|---|---|
| Knowledge | `get_data_dictionary`, `get_domain_knowledge`, `capture_finding` |
| Discovery | `list_databases`, `list_tables`, `describe_table`, `count_rows` |
| Entity resolution | `resolve_label`, `resolve_client` |
| SQL | `sql_db_query_checker`, `sql_db_query` |
| UX | `visualize_results`, `note_default_applied` |

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

No AWS calls, no LLM calls, no network. Covers the data-dictionary index
(parsed from a README owned by another repo, so it can break from outside),
the auth bypass gates, config accessors, and the module wiring.

## Planned work

See [ROADMAP.md](ROADMAP.md). The near-term items are persisting conversation
history (currently lost on every restart) and migrating the UI layer to
Chainlit. The module split above is what keeps that migration bounded: only
`app.py`, `ui.py`, `auth.py` and `results.py` touch Streamlit at all.

## Deployment

Unchanged by the module split: the Dockerfile copies the whole directory and
runs `streamlit run app.py`; the release tarball ships the whole directory.
There is no packaging step and no install.

Two exclusions to remember:

- **`.env`** is excluded from both the tarball and the extraction on the box.
- **`knowledge/`** is excluded from extraction and is a Docker volume, so a
  refreshed data dictionary must be copied to the instance directly — a deploy
  will not carry it.
