# Deploying the Data Lake Agent

How the agent is deployed and run on EC2. For how the code is laid out, see
[ARCHITECTURE.md](ARCHITECTURE.md); for what is planned, [ROADMAP.md](ROADMAP.md).

## What runs

One Docker image, one service, on one EC2 instance (Amazon Linux 2023,
Docker Compose):

| Service | Host port | App |
|---|---|---|
| `datalake-agent` | 8501 | Chainlit (`chainlit_app.py`, port 8000 in the container) |

**The Chainlit app replaces the Streamlit one.** Same service and container
name, and the same host port, 8501, so users' address and the security group
don't change. The Streamlit code (`app.py`, `ui.py`, `auth.py`) is still in
the repo but no longer run; it goes when the project moves to the deployment
repo. The `chat-db` (Postgres) and `chat-dynamodb`
(DynamoDB Local) services in `docker-compose.yml` are for local development
only (profile `local-db`), and never start on a server.

State that lives outside the image:

| What | Where | Survives a deploy because |
|---|---|---|
| Server settings and secrets | `.env` on the instance | Excluded from git and from the image |
| Data dictionary and the agent's findings | `knowledge/` on the instance, a volume | `remote_deploy.sh` skips it (see "Updating the knowledge files") |
| Chat history, favourites, usage, tiers | `CHAT_STORE=dynamodb`: the DynamoDB table. `CHAT_STORE=sql`: the database in `CHAT_DB_URL` (unset = SQLite in `data/`, a volume) | It's outside the instance, or a volume |

---

## Before the first Chainlit deploy

### Decided

- **Database: DynamoDB** (`CHAT_STORE=dynamodb`, section 1.6). The team
  doesn't want to run RDS. The SQL store is kept and selectable
  (`CHAT_STORE=sql`) in case RDS is revisited. Nothing needs migrating:
  nothing has been deployed with history.
- **Chainlit replaces Streamlit,** at the same address (above).
- **Sign-in: the same as the Streamlit app.** It used Google SSO when
  `.streamlit/secrets.toml` on the server had a Google client, and the
  shared password otherwise. Chainlit does the same from `.env`: Google SSO
  when `OAUTH_GOOGLE_CLIENT_ID` is set, and the password (`APP_PASSWORD`)
  either way. See section 2.

### Changes needed in the repo

- [x] **`.dockerignore`** keeps `.env`, `.venv/`, `data/` and the tests out
  of the image.
- [ ] **Restrict `allow_origins`** in `.chainlit/config.toml` from `["*"]` to
  the address users open (the Streamlit app's), once confirmed.
- [ ] **Only if history ever runs on SQLite:** check `remote_deploy.sh` skips
  `data/` as well as `knowledge/`, so a deploy can't overwrite it. Not
  needed on DynamoDB. The script lives with the CI setup, not in this repo.

---

## 1. AWS setup (once)

### 1.1 EC2 instance

| Setting | Value |
|---|---|
| AMI | Amazon Linux 2023 |
| Instance type | `t3.small` (`t3.medium` for heavier use) |
| Storage | 20 GB gp3 |
| Key pair | The team's existing `.pem` key |

### 1.2 Security group

| Port | Source | For |
|---|---|---|
| 22 | Admin IPs | SSH |
| 8501 | Internal range / VPN CIDR | The app, as today |
| 443 | Internal range / VPN CIDR | Only if a load balancer goes in front (1.4) |

Never `0.0.0.0/0`.

### 1.3 Instance role

The app uses the instance role; no AWS keys go in `.env`. It needs:

- **Athena** on the `datalake-agent` workgroup: `StartQueryExecution`,
  `GetQueryExecution`, `GetQueryResults`, `StopQueryExecution` (a query
  that times out is stopped, not abandoned).
- **S3** on `s3://tg-temp-data/athena-results/datalake-agent/`: read and
  write (Athena writes results there; the app reads them for downloads and
  checks their size first).
- **S3 read** on the data lake buckets behind the `tg-deltalake-bronze`,
  `tg-deltalake-silver` and `tg-master` tables. Athena reads them with the
  caller's permissions. Bucket names not listed here.
- **Glue** read: `GetDatabase(s)`, `GetTable(s)`, `GetPartitions`.
- **Bedrock** `bedrock-mantle:CreateInference`, for GLM-5, the default
  model.

The `datalake-agent` workgroup itself enforces the 50 GB per-query scan
cutoff and its own results location. Don't run the agent in `primary`: it
has no cutoff and is shared with the reporting pipelines.

### 1.4 HTTPS

Nothing new: the app is served exactly as the Streamlit app was. If that
already sits behind a load balancer, keep it, forwarding to the instance on
8501. Chainlit uses a websocket; an ALB's default 60 s idle timeout is fine,
as Socket.IO sends a heartbeat every 25 s.

### 1.5 Google SSO (only if the Streamlit app used it)

Check the server's `.streamlit/secrets.toml`: an `[auth]` section with a
Google `client_id` means the Streamlit app signed people in with Google.
If so, reuse that same Google OAuth client:

- In Google Cloud console → Credentials → that client, **add** the redirect
  URI `<the app's address>/auth/oauth/google/callback`. Leave Streamlit's
  `…/oauth2callback` until the switch-over is done.
- Copy its client id and secret into the server's `.env` as
  `OAUTH_GOOGLE_CLIENT_ID` and `OAUTH_GOOGLE_CLIENT_SECRET`, and set
  `CHAINLIT_URL` to the app's address (section 2).

Only verified `@tunedglobal.com` accounts get in; others see Chainlit's
sign-in error page. Without a Google client, sign-in is the shared password,
as it was for Streamlit.

### 1.6 Database

**DynamoDB (`CHAT_STORE=dynamodb`).** One table, in the same region as
`AWS_REGION` (`us-west-2`), created by whoever owns the account. On AWS the
app only checks it exists, so the instance needs no `CreateTable`
permission; if it's missing, Chainlit logs "DynamoDB table ... does not
exist" at startup:

```
Table   datalake-agent-chat     on-demand (PAY_PER_REQUEST), point-in-time recovery on
Keys    PK (S, hash)            SK (S, range)
Index   UserThread              UserThreadPK (S, hash)  UserThreadSK (S, range)  projection ALL
TTL     expiresAt
```

The definition is also `chat_store_dynamo.table_definition()`; the name is
the app's default on the server (local runs default to
`test-datalake-agent-chat` instead), so the server needs no `DYNAMODB_TABLE`. The instance
role needs, on `arn:aws:dynamodb:us-west-2:<account>:table/datalake-agent-chat`
and `…/index/*`: `GetItem`, `PutItem`, `UpdateItem`,
`DeleteItem`, `Query`, `BatchGetItem`, `BatchWriteItem`, `DescribeTable`.
Retention is TTL: a chat's items expire `CHAT_RETENTION_DAYS` after its last
question or opening; favourites never expire. Point-in-time recovery is the
backup.

**SQL (`CHAT_STORE=sql`),** kept for if RDS is revisited: RDS PostgreSQL in
the production account, reachable from the instance. Create an empty
database and a user that owns it; the app creates its tables on startup.
`CHAT_DB_URL=postgresql+asyncpg://user:pass@host:5432/db?ssl=require`.

---

## 2. The server's `.env`

Start from `.env.example`. The settings that matter on a server:

| Setting | Value | Notes |
|---|---|---|
| `AWS_PROFILE` | *(empty)* | Use the instance role |
| `AWS_REGION` | `us-west-2` | |
| `ATHENA_WORKGROUP` | `datalake-agent` | **Check an existing `.env` doesn't still say `primary`**: it would override the default and run with no scan cutoff |
| `ATHENA_OUTPUT_S3` | `s3://tg-temp-data/athena-results/datalake-agent/` | |
| `CHAINLIT_AUTH_SECRET` | from `chainlit create-secret` | Required. Changing it signs everyone out |
| `APP_PASSWORD` | a strong shared password | Plus a `@tunedglobal.com` email to sign in |
| `CHAINLIT_URL` | the app's address, e.g. `https://<host>` | Needed for Google SSO's redirect (1.5) |
| `OAUTH_GOOGLE_CLIENT_ID` / `_SECRET` | the Streamlit app's Google client (1.5) | Leave empty if it used the password only |
| `CHAT_STORE` | `dynamodb` | `sql` (the default) uses `CHAT_DB_URL` instead; `off` disables history |
| `DYNAMODB_TABLE` | leave unset | Inside the server's container the app uses `datalake-agent-chat`, the table from 1.6. Anywhere else it defaults to `test-datalake-agent-chat`, so local runs can't reach production by accident |
| `USAGE_RETENTION_DAYS` | `0` (default: keep) | Athena usage rows on DynamoDB; e.g. `400` for 13 months |
| `CHAT_DB_URL` | only with `CHAT_STORE=sql`: empty (SQLite) or `postgresql+asyncpg://user:pass@host:5432/db?ssl=require` | `off` disables history |
| `CHAT_RETENTION_DAYS` | `60` (default) | Non-favourite chats are deleted this long after they were last asked or opened (DynamoDB TTL; on SQL, a daily sweep by last message). `0` keeps everything |
| `FAVOURITES_MAX` | `20` (default) | Favourite chats per user; favourites are never deleted, so this bounds them. `0` = no cap |
| `CHAT_MAX_QUESTIONS` | `25` (default) | Questions per chat, then the user starts a new one. Matches the 25 past exchanges the model is sent. `0` = no cap |
| `ATHENA_SESSION_SCAN_BUDGET_GB` | `50` (default) | Per chat; the user is asked before going over |
| `DOWNLOAD_MAX_MB` | `100` (default) | Larger results get no download |
| `OPENAI_API_KEY`, `GEMINI_API_KEY` | optional | Only for those entries in the model picker; GLM-5 needs none |

**Never set `DEV_SKIP_AUTH` on a server.** The app ignores it in a
container and logs an error, but it doesn't belong there.

---

## 3. First install

```bash
scp -i <key>.pem -r app ec2-user@<instance>:~/app
ssh -i <key>.pem ec2-user@<instance>
cd ~/app
bash ec2-setup.sh       # installs Docker and Compose; stops to let you edit .env
nano .env               # section 2
docker compose up -d --build
```

`ec2-setup.sh` prints the app's address, on 8501.

---

## 4. Deploying an update

Normally CI deploys a release with `remote_deploy.sh` (kept with the CI
setup, not in this repo). By hand:

```bash
cd ~/app
git pull                    # or scp the changed files
docker compose up -d --build
```

Neither carries `knowledge/` or `.env`; see below for the knowledge files.

### Updating the knowledge files

`knowledge/` is a volume, and deploys skip it, so a changed data dictionary
must be copied to the instance:

```bash
scp -i <key>.pem -r app/knowledge/data-dictionary ec2-user@<instance>:~/app/knowledge/
```

No restart needed: the dictionary is read on every lookup. **Don't copy
`knowledge/feedback.md`** over the server's: the server's copy holds what
the agent captured there.

---

## 5. Checking a deploy

```bash
docker compose ps                                                  # both services Up
docker compose exec datalake-agent printenv ATHENA_WORKGROUP    # datalake-agent
docker compose exec datalake-agent printenv CHAT_STORE          # dynamodb
curl -s http://localhost:8501/health                            # the app is serving
docker compose logs --tail 50 datalake-agent                    # no errors, none about the table
```

Then in a browser:

- [ ] Sign in (password, and SSO if set up).
- [ ] Ask a question that returns a table: the answer has a table and CSV and Excel downloads.
- [ ] The usage beside the model picker goes up.
- [ ] Reopen that chat from the sidebar: the table and downloads are still there.
- [ ] Favourite a chat; it moves to the Favourites section.

---

## Day-to-day operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f datalake-agent` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Rebuild after a code change | `docker compose up -d --build` |
| Read the agent's findings | `cat knowledge/feedback.md`, or `/view-feedback` in Chainlit's dev mode |
| Make a user uncapped | `docker compose exec datalake-agent python -c "import asyncio, storage; asyncio.run(storage.store_from_env().set_user_tier('someone@tunedglobal.com', 'uncapped'))"` |

Backups: DynamoDB's point-in-time recovery (1.6). On SQL: back up `data/` for SQLite; RDS has its own.

## How sign-in and sessions behave

- Sign-in lasts 24 hours (`user_session_timeout` in `.chainlit/config.toml`),
  then the user signs in again.
- A dropped connection keeps its session for an hour (`session_timeout`). An
  answer that finished while the browser was away is shown when it
  reconnects.
