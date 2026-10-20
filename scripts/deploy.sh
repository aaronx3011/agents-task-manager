#!/usr/bin/env bash
# Deploy the coordinator on this host. Run from the repository root that the
# runner rsync'd, e.g.: bash scripts/deploy.sh development
# Idempotent: builds, (re)creates only changed containers, migrates, waits for
# health, provisions the development admin account once. Never deletes volumes.
set -euo pipefail

ENVIRONMENT="${1:-development}"
SHA="${2:-unknown}"
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$BASE_DIR/deploy/$ENVIRONMENT"

cd "$BASE_DIR"
echo "==> Deploying $ENVIRONMENT at commit $SHA"
[[ -f "$DEPLOY_DIR/compose.yaml" ]] || { echo "Unknown environment: $ENVIRONMENT" >&2; exit 1; }

compose() { docker compose -f "$DEPLOY_DIR/compose.yaml" "$@"; }

if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
  echo "==> Generating one-time secrets in $DEPLOY_DIR/.env (chmod 600)"
  umask 177
  {
    echo "DJANGO_SECRET_KEY=$(openssl rand -hex 48)"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    echo "DEV_ADMIN_PASSWORD=$(openssl rand -hex 12)"
  } > "$DEPLOY_DIR/.env"
  umask 022
fi
set -a; source "$DEPLOY_DIR/.env"; set +a

echo "==> Building images"
compose build

echo "==> Starting stack"
compose up -d --wait --wait-timeout 300

echo "==> Applying migrations"
compose exec -T web python manage.py migrate --noinput

if [[ "$ENVIRONMENT" == "development" ]]; then
  echo "==> Ensuring development admin account"
  compose exec -T -e DEV_ADMIN_PASSWORD="$DEV_ADMIN_PASSWORD" web python manage.py shell -c '
from django.conf import settings
from django.contrib.auth import get_user_model

assert settings.DEBUG is False
user, created = get_user_model().objects.get_or_create(
    username="aaron", defaults={"is_staff": True, "is_superuser": True},
)
user.is_staff = user.is_superuser = True
if created:
    user.set_password(settings.__dict__.get("DEV_ADMIN_PASSWORD") or __import__("os").environ["DEV_ADMIN_PASSWORD"])
user.save()
print("admin ready:", user.username, "(created)" if created else "(existing)")'
fi

echo "==> Health check"
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/healthz | grep -q ok; then
    echo "Deployment OK: $ENVIRONMENT @ $SHA"
    docker compose -f "$DEPLOY_DIR/compose.yaml" ps
    exit 0
  fi
  sleep 2
done
echo "Health check FAILED" >&2
docker compose -f "$DEPLOY_DIR/compose.yaml" logs --tail 50 web >&2
exit 1
