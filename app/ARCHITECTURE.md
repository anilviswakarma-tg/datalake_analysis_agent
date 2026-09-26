# Architecture

A Streamlit app that turns plain-English questions into AWS Athena SQL, runs
them, and shows the results. Entry point is `app.py`; run it with
`streamlit run app.py`.

## Modules

Listed in dependency order. **A module may only import ones above it** — that
rule is what keeps the graph acyclic, and `tests/test_wiring.py` enforces it.

| Module | Lines | Responsibility |
|---|---|---|
| `config.py` | ~90 | Environment defaults, filesystem paths, database-name accessors. Imports nothing else here. |
| `aws.py` | ~160 | boto3 sessions and clients, credential retry, the Athena query executor, S3 result fetch. The only module that talks to AWS. |
| `results.py` | ~125 | Chart selection and rendering (Altair), CSV/Excel export bytes. |
| `run_state.py` | ~55 | The mutable handoff from agent tools to the UI. |
| `knowledge.py` | ~170 | Reading the data dictionary, `domain_rules.md` and `feedback.md`. |
| `entities.py` | ~90 | Resolving names ("Sony", "Etisalat") to `owner_id` / `group_id`. |
| `models.py` | ~120 | Model registry, Bedrock Mantle SigV4 auth, the active-model choice. |
| `prompt.py` | ~165 | The agent system prompt. |
| `tools.py` | ~510 | The 13 tools the agent can call. |
| `agent.py` | ~40 | Assembles model + tools + prompt into the LangChain agent. |
| `ui.py` | ~205 | Styling, the agent catalogue, conversation buckets, rendering a run. |
| `auth.py` | ~195 | Login screen, SSO/password paths, session expiry, dev bypass. |
| `app.py` | ~360 | Page composition and the agent run loop. Entry point. |

### Why the boundaries fall where they do

- **`run_state.py` is separate** because agent tools execute inside the agent
  loop with no access to the Streamlit call stack. They deposit results in a
  module-level dict; the UI collects them once the run finishes. Isolating
  that shared mutable state makes the coupling visible rather than incidental.
- **`models.py` is split from `agent.py`** to break a cycle: `agent` needs the
  tool list, and `tools.sql_db_query_checker` needs to build an LLM with the
  user's selected model. Both depend on `models`, and neither on the other.
  The choice is shared through `set_active_model()` / `build_active_llm()`
  rather than a cross-module global, which does not work.
- **`config.py` imports nothing** from this package, so every other module can
  depend on it freely.

## Knowledge sources

Precedence when they disagree — enforced in `prompt.py` and documented in
`knowledge/README.md`:

1. **`knowledge/data-dictionary/`** — authoritative on what tables and columns
   *mean*. Vendored from `reporting-deltalake/docs/data-dictionary`; maintained
   there, not here. Read via `get_data_dictionary(tables)`.
2. **AWS Glue** — authoritative on what *exists* and its type, via
   `describe_table`.
3. **`knowledge/domain_rules.md`** — the agent playbook: mandatory filters,
   default date ranges, SQL patterns, reporting rules. Loses to both.
4. **`knowledge/feedback.md`** — append-only observations from the agent,
   for human review and promotion.

`knowledge/dictionary_gaps.md` tracks what `domain_rules.md` knows that the
dictionary doesn't yet.

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
