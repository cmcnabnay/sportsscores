#!/usr/bin/env bash
# fetch_and_push.sh
#
# Runs fetch_fixtures.py, then commits + pushes fixtures.json to GitHub
# if (and only if) it actually changed. Meant to be called by
# fetch-fixtures.service instead of calling python3 directly.

set -euo pipefail

REPO_DIR="/home/cmcnabnay/sportsscores"
cd "$REPO_DIR"

echo "=== Running fetch_fixtures.py ==="
/usr/bin/python3 fetch_fixtures.py

# Stage just fixtures.json - don't sweep up unrelated local changes
git add fixtures.json

# If nothing changed, diff --cached --quiet exits 0 (true) and we skip out
if git diff --cached --quiet -- fixtures.json; then
    echo "=== No changes to fixtures.json, nothing to commit ==="
    exit 0
fi

echo "=== Changes detected, committing and pushing ==="
git commit -m "Update fixtures.json ($(date -u '+%Y-%m-%d %H:%M UTC'))"

# Timeout guards against a hung credential prompt when run non-interactively
# (e.g. after logout, if the credential helper can't supply a token silently)
if ! timeout 30 git push; then
    echo "!! git push failed or timed out - check credential helper" >&2
    exit 1
fi

echo "=== Push complete ==="
