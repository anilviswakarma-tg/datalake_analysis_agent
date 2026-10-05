# Roadmap

Planned work, in priority order. See [ARCHITECTURE.md](ARCHITECTURE.md) for how
the code is laid out today.

---

## 1. Persist conversation history — *reported by users*

**The problem.** Everything a user has asked and every answer they've received
lives in `st.session_state`, which is server RAM scoped to one browser session.
It is lost on:

- a container restart or any deploy,
- a browser refresh (a new session is created),
- switching device or browser — history never follows the user.

There is no persistence layer at all. 44 `session_state` references across
`app.py`, `auth.py` and `ui.py` are the entire state story.

**What a run actually holds** (`app.py`, the `runs.append(...)` block):

| Field | Persist? | Why |
|---|---|---|
| `question`, `answer` | yes | the conversation itself |
| `query_id` | yes | the key to everything else |
| `trace`, `chart`, `notices` | yes | small JSON, needed to re-render |
| `dataframe` | **no** | rebuild from S3 — see below |
| `messages` | optional | raw agent messages, Dev mode only; awkward to serialise |

**The design.** History must NOT depend on Athena results living forever —
results get a lifecycle policy (see below). Instead:

- **Store a preview inline.** `sql_db_query` only ever reads 100 rows into the
  app (`tools.py`: `_run_athena_query(query, limit_rows=100)`); the full CSV
  exists solely to feed the download buttons. So persisting those same ~100
  rows as JSON costs a few KB per run and preserves the on-screen experience
  permanently — table, chart and all.
- **Store the SQL** alongside it.
- **Full download while the result still exists.** `aws._fetch_s3_csv(query_id)`
  already does this. Once the S3 object has aged out, the UI offers **re-run
  this query** instead of a dead download button. That is arguably the better
  behaviour anyway: the lake refreshes daily (hourly for `track_active`), so a
  months-old CSV is stale data presented as current.

This decouples the two retentions completely: **history is tiny and kept
indefinitely; results expire on a schedule.**

*(Superseded 2026-10-05: the agent now runs in its own `datalake-agent`
workgroup, which enforces `s3://tg-temp-data/athena-results/datalake-agent/`
and a 50 GB per-query cutoff. The rest of this paragraph is the earlier
setup.)* Results went to `s3://athena-results-223829094007-us-west-2/`, set by
`ATHENA_OUTPUT_S3` and passed as `ResultConfiguration.OutputLocation` on every
query. The `primary` workgroup cannot override it
(`EnforceWorkGroupConfiguration: false`, no output location of its own).

> ⚠️ **Verify the bucket on the production box before building on it.** The
> above reflects the *committed* config. The running container reads its own
> `.env` on the EC2 instance, excluded from git and from deploys and never
> reviewed here. Confirm with:
> `docker compose exec datalake-agent printenv ATHENA_OUTPUT_S3`

### Add an S3 lifecycle policy

Expire objects under the results prefix after **30 days** (raise to 90 if
download-from-history matters more than tidiness). Standard practice for Athena
output, and it caps growth permanently.

Sizing note, so the decision is made on numbers: the bucket is currently
**0.09 GB / 3,004 objects ≈ $0.002 per month**, and the largest result ever
written is 8.8 MB. Even a thousandfold increase would be about **$2/month**.
Storage is not where this application's AWS spend is, or plausibly could be —
see the scan-cost issue below, which is the one worth acting on. The lifecycle
policy is still worth adding as hygiene; it just shouldn't be mistaken for a
cost control.

**Proposed shape**

- New `history.py`. **No Streamlit import** — this is deliberate, see Phase 2.
- SQLite on a mounted Docker volume, WAL mode. Fine for this concurrency; no
  new infrastructure.
- Two tables:
  - `threads(id, user_email, agent_key, title, created_at, updated_at)`
  - `runs(id, thread_id, question, answer, query_id, chart_json, notices_json, trace_json, created_at)`
- Keyed on `user_email`, which auth already resolves.
- `session_state` becomes a cache in front of it rather than the source of truth.

**Deployment notes**

- Add a `data/` volume to `docker-compose.yml`, alongside the existing
  `knowledge/` one.
- Add `--exclude='data'` to the extraction in `remote_deploy.sh`, so a deploy
  can never clobber the database. (It shouldn't today — `data/` would be
  gitignored and therefore absent from the CI checkout — but relying on that is
  fragile.)
- Back it up, or move to DynamoDB/RDS, before this box becomes a single point of
  failure for anything anyone cares about keeping.

**Effort:** 1–2 days. **Unblocks the reported complaint without waiting for
Phase 2.**

---

## 2. Migrate the UI to Chainlit

**Why.** Streamlit is fighting this application's shape. The whole-script-rerun
model is why `run_state._LAST_RESULT` exists as a module global (tools run
inside the agent loop with no access to the Streamlit call stack), why the
`_runs_bucket_key` / `st.rerun()` choreography exists, and why session expiry is
implemented as an injected JavaScript `setTimeout` — via
`st.components.v1.html`, which Streamlit now warns was slated for removal
**after 2026-06-01**, a date already passed. That will break on some upgrade.

Chainlit is purpose-built for this exact shape: an LLM chat app with streaming,
tool steps and history.

| Need | Streamlit today | Chainlit |
|---|---|---|
| Chat history across restarts | none (Phase 1 builds it) | built-in data layer |
| Token streaming | hand-rolled in `app.py` | native |
| Tool trace (Dev mode) | custom tabs + expanders | native step visualisation |
| Auth | hand-rolled login + deprecated JS timer | auth callbacks / OAuth |
| Underlying server | Streamlit runtime | FastAPI — real routes, health checks |

**Scope is bounded, and that's measurable.** 67% of the code has zero Streamlit
coupling:

| | Lines | Streamlit calls |
|---|---|---|
| Untouched — config, aws, run_state, knowledge, entities, models, prompt, agent, tools | **1,526** | 1 |
| Rewritten — app, auth, ui, results | **764** | 156 |

The agent, tools, knowledge layer, AWS access and entity resolution move across
unchanged. This is a UI-layer project, not a rewrite.

**Why Phase 1 isn't throwaway.** Chainlit's data layer is SQLAlchemy-based, so
SQLite (or Postgres later) carries over, and `history.py` deliberately has no
framework dependency. Worst case the schema is remapped; the storage choice and
the rehydrate-from-S3 design both survive.

**Verify before committing** — these need checking against current Chainlit
docs rather than taken on trust:

- Does the four-agent landing page with suggested-question tiles map cleanly
  onto chat profiles / starters?
- Custom branding: Chainlit theming is more constrained than injecting
  arbitrary CSS. Is the Tuned Global look achievable?
- DataFrame tables and CSV/Excel downloads will need custom elements.
- Confirm the data layer's storage options and auth callbacks match what's
  described above.

**Effort:** days, not weeks — but do Phase 1 first so users stop losing work
while this is in progress.

### Status (2026-09-26) — built alongside Streamlit, not yet deployed

`chainlit_app.py` on Chainlit 2.12.0, checked end to end in a browser against
live Athena. The prerequisite is done: run state is a per-run `ContextVar`
(`run_state.start_run`), not a process global.

Answers to the checks above:

- **Landing page and branding: kept as they were (2026-09-30).** Chat
  profiles and starters were tried first and dropped: they meant a header
  dropdown and pill-shaped suggestions, a visibly different app, and starters
  always send the default model. Instead `public/app.js` + `public/app.css`
  redraw the Streamlit layout (hero, agent tiles, question tiles, sidebar
  AGENTS list and signed-in footer, title bar) from `public/agents.json`,
  hooking onto Chainlit's element ids. Questions go through the real message
  box, so they use the picked model. Colours, logo and fonts match the
  Streamlit theme. What still differs: the model picker and the Dev mode /
  dry-run switches sit under the message box (⚙️), not in a top bar; the
  sidebar also lists past chats; and in dev mode the tool trace shows as
  steps above the answer instead of tabs.
- **Tables and downloads: native.** `cl.Dataframe`, `cl.File`. Charts moved
  from Altair to Plotly (`results.plotly_figure`), since Chainlit has no Vega.
- **Auth: matches.** Password callback (domain + `APP_PASSWORD`), Google
  OAuth callback when `OAUTH_GOOGLE_CLIENT_ID` is set (domain checked in the
  callback — Chainlit doesn't send Google's `hd` hint), and the dev bypass as a
  header-auth callback behind the same container gate. The 24-hour expiry is
  `user_session_timeout`; no JavaScript timer.
- **Data layer: done, database-agnostic (2026-09-26).** SQLite on a Docker
  volume for now, selected by `CHAT_DB_URL`; moving to Postgres is a URL
  change. The stock `SQLAlchemyDataLayer` silently dropped user messages and
  thread metadata on SQLite and Chainlit's DDL misses `autoCollapse` — see
  `chainlit_data.py` and `chat_store.py`. Results are rebuilt on resume from
  a snapshot saved with each answer, so no blob store is needed — Phase 1's
  design, implemented.

- **Retention (2026-09-30).** Chats are deleted after 60 days without
  activity (`CHAT_RETENTION_DAYS`) unless the user makes them favourites
  (the title bar's Favourite button or the chat's ⋯ menu); favourites show a
  star in the chat list. The
  sweep runs at startup and daily. The model is sent the last 25 exchanges
  of a chat (`MAX_HISTORY`).

Remaining before Streamlit can be removed: make Chainlit the `Dockerfile`
default (`docker-compose.yml` now runs both, Chainlit on 8000 with the
`data/` volume), and restrict `allow_origins` in `.chainlit/config.toml` to
the real host.

**Usage tiers (2026-10-05): capped and uncapped, caps not enforced yet.**
Every user is capped unless `user_tiers` marks them uncapped. There is no
admin screen; to change one:

```bash
python -c "import asyncio, chat_store; asyncio.run(chat_store.set_user_tier(chat_store.chat_db_url(), 'someone@tunedglobal.com', 'uncapped'))"
```

Next: a per-user monthly limit column on `user_tiers`, checked against
`chat_store.month_usage` before a question runs.

**TODO: set `ATHENA_WORKGROUP=datalake-agent` in the server's `.env`** (and
`ATHENA_OUTPUT_S3` to its results location). The code defaults to it, but a
server `.env` still saying `primary` would override that and run with no scan
cutoff. Details and IAM needs in [DEPLOY.md](DEPLOY.md), Step 4.

---

## 3. Optional: a Slack entry point

The audience is internal non-technical staff who currently have to remember to
visit a URL. A Slack command wrapping the same agent would likely see
substantially more use, because it lives where they already work. It reuses the
same 1,526 framework-agnostic lines.

This is a distribution question, not a technology one — worth weighing against
Phase 2 rather than after it.

---

## Known issues

| Issue | Impact | Notes |
|---|---|---|
| `st.components.v1.html` deprecated past its removal date | session auto-logout breaks on a future Streamlit upgrade | `st.iframe` is the replacement; resolved by Phase 2 |
| **Mandated filters don't prune `mastermusic`** | **root cause of large scans.** One agent query on 2026-09-23 scanned 15.78 GB | `mastermusic` partitions on `owner_id_salt`/`dw_stock_type`, but the playbook pushes `owner_id` and claims it gives "partition pruning benefits" (`domain_rules.md:316`) — it doesn't. See [knowledge/dictionary_gaps.md](knowledge/dictionary_gaps.md) C1 |
| **No per-query scan cutoff** | an unbounded query can scan TBs at $5/TB | **don't set this on `primary`** — it's shared with the reporting pipelines and a cutoff there could break their jobs. Give the agent its own workgroup with `BytesScannedCutoffPerQuery`, its own output location (which also scopes the lifecycle policy to agent results), and `PublishCloudWatchMetricsEnabled` on. **App side done (2026-10-05):** `ATHENA_WORKGROUP` selects it; a cutoff-stopped query tells the agent how to narrow it; every query's bytes scanned are stored per user (`query_usage`, failed queries included) and the month's total shows beside the model picker |
| Athena results bucket has no lifecycle policy | 0.09 GB / 3,004 objects ≈ $0.002/month; largest object 8.8 MB | add 30-day expiry as hygiene. Phase 1 is designed not to depend on retention |
| Entity cache is in-memory, 15-min TTL | lost on restart | harmless, rebuilds on demand |
| `domain_rules.md` self-contradiction and dictionary gaps | agent may follow stale rules | tracked in [knowledge/dictionary_gaps.md](knowledge/dictionary_gaps.md) |
| Local dev can't reach Bedrock | can't test against GLM-5, the production model | needs `bedrock-mantle:CreateInference` on the dev IAM user |
