#!/bin/sh
set -eu

LOCAL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
FRONTEND_DIR="$LOCAL_DIR/../frontend"
API_HOST=${API_HOST:-127.0.0.1}
API_PORT=${API_PORT:-8000}
FRONTEND_HOST=${FRONTEND_HOST:-127.0.0.1}
FRONTEND_PORT=${FRONTEND_PORT:-5173}

cleanup() {
  kill "$API_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

python3 "$LOCAL_DIR/mock_api.py" --host "$API_HOST" --port "$API_PORT" &
API_PID=$!

cd "$FRONTEND_DIR"
API_BASE="http://${API_HOST}:${API_PORT}" \
  pnpm exec vite dev --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
FRONTEND_PID=$!

echo "Public app:     http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "Private app:    http://private.localhost:${FRONTEND_PORT}"
echo "Mock API:      http://${API_HOST}:${API_PORT}"
wait "$FRONTEND_PID"
