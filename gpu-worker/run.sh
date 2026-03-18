#!/usr/bin/env bash
#
# run.sh — Start the GPU worker agent.
#
# Polls the Cloudflare API for pending transcription jobs,
# downloads audio, runs faster-whisper via Docker, uploads results.
#
# Usage:
#   cd gpu-worker
#   ./run.sh                  # foreground (Ctrl+C to stop)
#   ./run.sh --status         # show pending jobs count, don't start worker
#   ./run.sh --rotate-token   # generate new token, update .env + Cloudflare secret
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
ENV_FILE="$PROJECT_DIR/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }
dim()   { echo -e "${CYAN}$*${NC}"; }

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

if [ ! -f "$ENV_FILE" ]; then
    fail "No .env file found at $ENV_FILE. See docs/DEPLOYMENT.md."
fi

set -a
source "$ENV_FILE"
set +a

if [ -z "${API_URL:-}" ]; then
    fail "API_URL not set in .env"
fi

if [ -z "${WORKER_TOKEN:-}" ]; then
    fail "WORKER_TOKEN not set in .env"
fi

# ---------------------------------------------------------------------------
# --rotate-token
# ---------------------------------------------------------------------------

if [ "${1:-}" = "--rotate-token" ]; then
    NEW_TOKEN=$(openssl rand -hex 32)

    info "Generating new worker token..."

    sed -i.bak "s/^WORKER_TOKEN=.*/WORKER_TOKEN=$NEW_TOKEN/" "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
    info "Updated $ENV_FILE"

    echo ""
    info "Setting Cloudflare secret..."
    (cd "$PROJECT_DIR/cloudflare/worker" && CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}" npx wrangler secret put WORKER_TOKEN <<< "$NEW_TOKEN" 2>&1) | tail -1

    echo ""
    info "New WORKER_TOKEN: $NEW_TOKEN"
    warn "Restart the worker with ./run.sh to pick up the new token."
    exit 0
fi

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then
    fail "docker not found. Run sudo ./setup.sh from the project root."
fi

if ! docker compose version >/dev/null 2>&1; then
    fail "docker compose plugin not found."
fi

if ! docker info 2>/dev/null | grep -q "Runtimes.*nvidia"; then
    warn "NVIDIA runtime not detected in Docker. GPU passthrough may not work."
    warn "Run sudo ./setup.sh to fix this."
fi

# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------

if [ "${1:-}" = "--status" ]; then
    echo "Checking for pending jobs at ${API_URL}..."
    RESP=$(curl -s -w "\n%{http_code}" \
        -H "Authorization: Bearer ${WORKER_TOKEN}" \
        "${API_URL}/api/jobs?status=pending&limit=50" 2>/dev/null || true)

    HTTP_CODE=$(echo "$RESP" | tail -1)
    BODY=$(echo "$RESP" | head -n -1)

    if [ "$HTTP_CODE" = "200" ]; then
        COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('jobs',[])))" 2>/dev/null || echo "?")
        echo ""
        echo "  Pending jobs: ${COUNT}"
        echo "  Worker URL:   ${API_URL}"
        echo "  Mode:         ${MODE:-docker}"
        echo "  Poll interval: ${POLL_INTERVAL:-5}s"
        echo ""

        if [ "$COUNT" != "0" ] && [ "$COUNT" != "?" ]; then
            echo "Jobs:"
            echo "$BODY" | python3 -c "
import sys, json
for j in json.load(sys.stdin).get('jobs', []):
    size_mb = j['file_size'] / 1024 / 1024
    print(f\"  {j['id'][:8]}...  {j['original_filename']:<30s} {size_mb:.1f} MB\")
" 2>/dev/null
        fi
    elif [ "$HTTP_CODE" = "401" ]; then
        fail "Authentication failed. Run ./run.sh --rotate-token to generate a new token."
    else
        fail "API returned HTTP ${HTTP_CODE}. Check API_URL in .env."
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Show summary and start
# ---------------------------------------------------------------------------

echo ""
echo "=========================================="
echo "  Whisper GPU Worker"
echo "=========================================="
echo ""

dim "  API URL:        ${API_URL}"
dim "  Worker ID:      ${WORKER_ID:-auto-generated}"
dim "  Mode:           ${MODE:-docker}"
dim "  Model:          ${WHISPER_MODEL:-turbo}"
dim "  Poll interval:  ${POLL_INTERVAL:-5}s"
dim "  Max concurrent: ${MAX_CONCURRENT_JOBS:-1}"
echo ""

# Kill any existing worker
OLD_PID=$(pgrep -f "python3 worker.py" 2>/dev/null || true)
if [ -n "$OLD_PID" ]; then
    warn "Existing worker found (PID ${OLD_PID}). Killing it..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
fi

info "Starting worker..."
echo ""

exec python3 "$SCRIPT_DIR/worker.py"
