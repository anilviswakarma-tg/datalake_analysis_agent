# EC2 + Docker Deployment Guide
## Tuned Global Data Lake Agent (internal)

---

## Prerequisites

- AWS account access
- An EC2 key pair (`.pem` file)
- Your OpenAI API key

---

## Step 1 — Launch an EC2 Instance

In the AWS Console → EC2 → Launch Instance:

| Setting | Value |
|---|---|
| AMI | **Amazon Linux 2023** |
| Instance type | `t3.small` (or `t3.medium` for heavier use) |
| Key pair | Your existing `.pem` key |
| Storage | 20 GB gp3 (default is fine) |

**Security Group** — create a new one with these inbound rules:

| Type | Port | Source | Purpose |
|---|---|---|---|
| SSH | 22 | Your IP | Admin access |
| Custom TCP | 8501 | Your company IP range (e.g. `10.0.0.0/8`) | App access (internal only) |

> Keep port 8501 restricted to your internal IP range or VPN CIDR — do NOT open it to `0.0.0.0/0`.

**IAM Instance Profile** — attach a role with these policies:
- `AmazonAthenaFullAccess`
- `AWSGlueConsoleFullAccess`
- Read access to your S3 Athena output bucket (or `AmazonS3FullAccess` for simplicity)

> With an IAM role attached, you do NOT need to set `AWS_PROFILE` or any AWS credentials in `.env`. The app detects the role automatically.

---

## Step 2 — Copy the App to EC2

From your local machine (in the `app_new` folder):

```bash
scp -i your-key.pem -r . ec2-user@<EC2-PUBLIC-IP>:~/app
```

---

## Step 3 — SSH into the Instance

```bash
ssh -i your-key.pem ec2-user@<EC2-PUBLIC-IP>
cd ~/app
```

---

## Step 4 — Configure Environment Variables

```bash
cp .env.example .env
nano .env   # or: vi .env
```

Set at minimum:
```
OPENAI_API_KEY=sk-...
AWS_PROFILE=          # leave blank — EC2 IAM role is used automatically
ATHENA_WORKGROUP=datalake-agent
ATHENA_OUTPUT_S3=s3://tg-temp-data/athena-results/datalake-agent/
```

> **TODO (before the next deploy): set `ATHENA_WORKGROUP` on the server.**
> The app defaults to `datalake-agent` (50 GB per-query scan cutoff), but an
> existing server `.env` that still says `ATHENA_WORKGROUP=primary` overrides
> that and runs the agent with no cutoff. Check with
> `docker compose exec datalake-agent-chainlit printenv ATHENA_WORKGROUP`.
> The instance role also needs Athena access to the `datalake-agent`
> workgroup, S3 read/write on `tg-temp-data/athena-results/datalake-agent/`,
> and `athena:GetWorkGroup` (for the usage bar's limit).

---

## Step 5 — Run the Setup Script

```bash
bash ec2-setup.sh
```

This installs Docker, Docker Compose, builds the image, and starts the container. It takes ~3 minutes on first run.

---

## Step 6 — Access the App

Open in your browser:
```
http://<EC2-PUBLIC-IP>:8501
```

---

## Day-to-day Operations

| Task | Command |
|---|---|
| View logs | `docker compose logs -f` |
| Stop | `docker compose down` |
| Restart | `docker compose restart` |
| Redeploy after code change | `docker compose up -d --build` |
| Update knowledge files | Edit `knowledge/domain_rules.md` directly — no restart needed |

---

## Updating the App

```bash
# From EC2, pull your latest code (if using git):
git pull

# Rebuild and restart:
docker compose up -d --build
```

Or re-copy files from your local machine with `scp` and run `docker compose up -d --build`.

---

## Notes

- **AWS auth on EC2**: The app uses the EC2 IAM instance role when `AWS_PROFILE` is blank — no SSO login needed on the server.
- **Knowledge files**: The `knowledge/` folder is mounted as a Docker volume, so edits to `domain_rules.md` or `feedback.md` persist across container restarts and rebuilds.
- **Internal only**: Keep the Security Group port 8501 restricted to your internal IP range. No authentication layer is added — this is suitable for a trusted internal network.
- **HTTPS**: For a more polished setup later, put an Application Load Balancer (with ACM certificate) in front and forward port 443 → 8501. But for internal use, HTTP is fine to start.
