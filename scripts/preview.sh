#!/usr/bin/env bash
# One-command preview restore: install deps (if missing), build the web
# demo (if missing), and serve it. Used after sandbox restarts and by
# anyone cloning the repo.
#
#   ./scripts/preview.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-4173}"

if [ ! -x node_modules/.bin/expo ]; then
  echo "▸ Installing dependencies…"
  npm install --ignore-scripts --no-audit --no-fund
fi

if [ ! -f webapp/index.html ]; then
  echo "▸ Building web demo…"
  EXPO_OFFLINE=1 CI=1 npx expo export --platform web --output-dir webapp
fi

echo "▸ Serving on http://localhost:${PORT}"
exec node scripts/serve-demo.js "$PORT"
