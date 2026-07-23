#!/usr/bin/env bash
# Start backend (FastAPI) + frontend (Next.js) for production on Linux.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
RUN_DIR="$ROOT/.run"
BACKEND_PORT="${PORT:-8010}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"

mkdir -p "$RUN_DIR"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Missing .venv — run: make install" >&2
  exit 1
fi

stop_pid_file() {
  local name="$1"
  local file="$RUN_DIR/${name}.pid"
  if [[ -f "$file" ]]; then
    local pid
    pid="$(cat "$file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "Stopping $name (pid $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$file"
  fi
}

stop_pid_file backend
stop_pid_file frontend

echo "Starting backend on 0.0.0.0:${BACKEND_PORT}..."
(
  cd "$ROOT/backend"
  export PYTHONPATH="$ROOT/backend"
  export PORT="$BACKEND_PORT"
  exec "$VENV/bin/python" -m uvicorn app:app --host 0.0.0.0 --port "$BACKEND_PORT"
) >"$RUN_DIR/backend.log" 2>&1 &
echo $! >"$RUN_DIR/backend.pid"

echo "Waiting for backend health..."
for _ in $(seq 1 30); do
  if curl -sf "${BACKEND_URL}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -sf "${BACKEND_URL}/api/health" >/dev/null 2>&1; then
  echo "Backend failed to start. Log:" >&2
  tail -n 30 "$RUN_DIR/backend.log" >&2 || true
  exit 1
fi

echo "Starting frontend on 0.0.0.0:${FRONTEND_PORT}..."
(
  cd "$ROOT/frontend"
  export BACKEND_URL="$BACKEND_URL"
  export HOST="0.0.0.0"
  export PORT="$FRONTEND_PORT"
  export NODE_ENV="production"
  exec npm start
) >"$RUN_DIR/frontend.log" 2>&1 &
echo $! >"$RUN_DIR/frontend.pid"

sleep 3
echo ""
echo "=== ASR-Agent is online ==="
echo "  App:     http://0.0.0.0:${FRONTEND_PORT}"
echo "  API:     ${BACKEND_URL}/api/health"
echo "  Logs:    ${RUN_DIR}/backend.log  ${RUN_DIR}/frontend.log"
echo "  Stop:    make stop"
echo ""
