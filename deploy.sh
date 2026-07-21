#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# community-finder — pull-from-GitHub deploy for the VPS.
#
# Run ON the VPS from the project dir:
#     cd /opt/community-finder && ./deploy.sh
#
# It aligns the checkout to the latest commit on the current branch (or the
# branch you pass as $1), rebuilds the images, and recreates the containers.
# Your local .env is never touched (it is gitignored).
#
#     ./deploy.sh                       # redeploy current branch
#     ./deploy.sh main                  # switch to + deploy main
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.prod.yml"
BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"

echo "==> Fetching origin/$BRANCH"
git fetch --prune origin "$BRANCH"

echo "==> Aligning working tree to origin/$BRANCH (tracked files only; .env preserved)"
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"

echo "==> HEAD now at: $(git log -1 --format='%h %s')"

echo "==> Building images"
$COMPOSE build

echo "==> Recreating containers"
$COMPOSE up -d

echo "==> Pruning dangling images"
docker image prune -f >/dev/null 2>&1 || true

echo "==> Status"
$COMPOSE ps
echo "==> Done — https://community-finder.unicornstudio.io"
