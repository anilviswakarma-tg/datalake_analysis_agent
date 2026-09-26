#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ec2-setup.sh — Run this ONCE on a fresh Amazon Linux 2023 EC2 instance
# to install Docker, copy the app, build, and start it.
#
# Usage (from your local machine):
#   1. scp -i your-key.pem -r ./app_new ec2-user@<EC2-IP>:~/app
#   2. ssh -i your-key.pem ec2-user@<EC2-IP>
#   3. cd ~/app && bash ec2-setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "==> Installing Docker..."
sudo dnf update -y
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo "==> Installing Docker Compose plugin..."
COMPOSE_VERSION="v2.27.0"
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

echo "==> Setting up .env from .env.example..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "  !! Edit .env now and set your OPENAI_API_KEY, then re-run:"
  echo "     docker compose up -d --build"
  echo ""
  exit 0
fi

echo "==> Building and starting the app..."
# Need to re-login for docker group to take effect; use 'sg' to apply immediately
sg docker -c "docker compose up -d --build"

echo ""
echo "  App is running at http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4):8501"
echo "  Logs: docker compose logs -f"
echo "  Stop: docker compose down"
