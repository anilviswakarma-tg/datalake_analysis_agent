# Deploying the Data Lake Agent

How the agent is deployed and run on EC2. For how the code is laid out, see
[ARCHITECTURE.md](ARCHITECTURE.md); for what is planned, [ROADMAP.md](ROADMAP.md).

## What runs

One Docker image, two services, on one EC2 instance (Amazon Linux 2023,
Docker Compose):

| Service | Port | UI | State |
|---|---|---|---|
| `datalake-agent` | 8501 | Streamlit (the original app) | Deployed currently |
| `datalake-agent-chainlit` | 8000 | Chainlit (the replacement) | **Not deployed yet** |

Both run the same agent code, so a deploy updates both. The image's default
command is still Streamlit; `docker-compose.yml` starts Chainlit from the same
image with its own command. The `chat-db` service in `docker-compose.yml` is
a local development database only (profile `local-db`), and never starts on
a server.

State that lives outside the image:

| What | Where | Survives a deploy because |
|---|---|---|
| Server settings and secrets | `.env` on the instance | Excluded from git and from the image |
| Data dictionary and the agent's findings | `knowledge/` on the instance, a volume | `remote_deploy.sh` skips it (see "Updating the knowledge files") |
| Chat history, favourites, usage, tiers | The database in `CHAT_DB_URL`; unset = SQLite in `data/`, a volume | It's a volume, or an external database |

---

## Before the first Chainlit deploy

### Decisions still open

| Decision | Options | Notes |
|---|---|---|
| Database | SQLite on the `data/` volume (the default, no setup), or RDS PostgreSQL | RDS is the plan (ROADMAP, "Postgres, locally"). Starting on SQLite is fine: nothing needs migrating yet, and switching later is a `CHAT_DB_URL` change, though chats saved on SQLite don't move across by themselves |
| Sign-in | Shared password only, or Google SSO as well | Google only accepts `http://` redirects for `localhost`, so SSO needs HTTPS (see step 1.4). Unverified against our OAuth client. Over plain HTTP the shared password travels unencrypted |
| Streamlit | Keep it on 8501 alongside Chainlit, or switch over | Switching means making Chainlit the `Dockerfile` default and removing the Streamlit service |

### Changes needed in the repo

- [ ] **Add a `.dockerignore`.** There is none, and the `Dockerfile` does
  `COPY . .`, so building on the server copies `.env` (every key and
  secret), `.venv/` and `data/` into the image. It needs at least: `.env`,
  `*.env`, `.venv/`, `data/`, `__pycache__/`, `.pytest_cache/`, `.files/`.
- [ ] **Restrict `allow_origins`** in `.chainlit/config.toml` from `["*"]` to
  the app's real address, once known.
- [ ] **Check `remote_deploy.sh` skips `data/`** as well as `knowledge/`, so
  a deploy can never overwrite the SQLite history. The script lives with the
  CI setup, not in this repo; not checked here.

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
| 8501 | Internal range / VPN CIDR | Streamlit, while it runs |
| 8000 | Internal range / VPN CIDR | Chainlit, without a load balancer |
| 443 | Internal range / VPN CIDR | Chainlit, behind a load balancer (1.4) |

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

### 1.4 HTTPS (needed for Google SSO)

An Application Load Balancer with an ACM certificate and a DNS name,
forwarding 443 to the instance on 8000. Chainlit uses a websocket; the
ALB's default 60 s idle timeout is fine, as Socket.IO sends a heartbeat
every 25 s. One instance, so no sticky sessions needed.

### 1.5 Google OAuth client (if SSO)

A Google Workspace OAuth client (web application) with redirect URI
`https://<host>/auth/oauth/google/callback`. Only verified
`@tunedglobal.com` accounts get in; others see Chainlit's sign-in error
page.

### 1.6 Database (if not SQLite)

RDS PostgreSQL in the production account, reachable from the instance.
Create an empty database and user; the app creates its tables on startup.

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
| `CHAINLIT_URL` | `https://<host>` | Needed behind a load balancer, for the OAuth redirect |
| `OAUTH_GOOGLE_CLIENT_ID` / `_SECRET` | from 1.5 | Leave empty for password-only |
| `CHAT_DB_URL` | empty (SQLite) or `postgresql+asyncpg://user:pass@host:5432/db` | `off` disables history |
| `CHAT_RETENTION_DAYS` | `60` (default) | Non-favourite chats idle this long are deleted daily; `0` keeps everything |
| `FAVOURITES_MAX` | `20` (default) | Favourite chats per user; favourites are never deleted, so this bounds them. `0` = no cap |
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

`ec2-setup.sh` prints the Streamlit address (8501); Chainlit is on 8000.

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
docker compose exec datalake-agent-chainlit printenv ATHENA_WORKGROUP    # datalake-agent
curl -s http://localhost:8000/health                               # Chainlit is serving
docker compose logs --tail 50 datalake-agent-chainlit              # no errors at startup
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
| Logs | `docker compose logs -f datalake-agent-chainlit` (or `datalake-agent`) |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Rebuild after a code change | `docker compose up -d --build` |
| Read the agent's findings | `cat knowledge/feedback.md`, or `/view-feedback` in Chainlit's dev mode |
| Make a user uncapped | see "Usage tiers" in [ROADMAP.md](ROADMAP.md) |

Back up `data/` while history is on SQLite; RDS has its own backups.

## How sign-in and sessions behave

- Sign-in lasts 24 hours (`user_session_timeout` in `.chainlit/config.toml`),
  then the user signs in again.
- A dropped connection keeps its session for an hour (`session_timeout`). An
  answer that finished while the browser was away is shown when it
  reconnects.
