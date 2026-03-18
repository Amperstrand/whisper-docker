#!/usr/bin/env bash
#
# cleanup.sh — Remove jobs and their R2 storage.
#
# Usage:
#   ./scripts/cleanup.sh [DAYS]
#
#   DAYS: Delete jobs older than this many days (default: 0 = all jobs)
#
# Requires: wrangler authenticated (or CLOUDFLARE_API_TOKEN in .env)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
ENV_FILE="$PROJECT_DIR/.env"
DAYS="${1:-0}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

if [ ! -f "$ENV_FILE" ]; then
    fail ".env file not found at $ENV_FILE. Run deploy.sh first."
fi

set -a
source "$ENV_FILE"
set +a

D1_NAME="${D1_DATABASE_NAME:-whisper-transcriptions}"
R2_NAME="${R2_BUCKET_NAME:-whisper-transcriptions}"

# Build the age filter
if [ "$DAYS" -eq 0 ]; then
    AGE_FILTER="1 = 1"
    AGE_DESC="all jobs"
else
    AGE_FILTER="created_at < datetime('now', '-${DAYS} days')"
    AGE_DESC="jobs older than $DAYS day(s)"
fi

echo "Finding $AGE_DESC..."

# Query D1 for matching jobs
ROWS=$(CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}" \
    npx --prefix "$PROJECT_DIR/cloudflare/worker" wrangler d1 execute "$D1_NAME" --remote \
    --command="SELECT id, status, original_filename, created_at FROM jobs WHERE $AGE_FILTER ORDER BY created_at ASC" \
    --json 2>/dev/null || echo "[]")

COUNT=$(echo "$ROWS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)[0].get('results',[])))" 2>/dev/null || echo "0")

if [ "$COUNT" = "0" ]; then
    info "No jobs found."
    exit 0
fi

echo "Found $COUNT job(s) to delete:"
echo ""
echo "$ROWS" | python3 -c "
import sys, json
for row in json.load(sys.stdin)[0].get('results', []):
    print(f\"  {row['id'][:8]}...  {row['status']:<12s} {row['original_filename']:<30s} {row['created_at']}\")
" 2>/dev/null
echo ""

read -rp "Delete $COUNT job(s)? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Cancelled."
    exit 0
fi

IDS=$(echo "$ROWS" | python3 -c "
import sys, json
for row in json.load(sys.stdin)[0].get('results', []):
    print(row['id'])
" 2>/dev/null)

DELETED=0
FAILED=0

for id in $IDS; do
    # Delete from D1
    CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}" \
        npx --prefix "$PROJECT_DIR/cloudflare/worker" wrangler d1 execute "$D1_NAME" --remote \
        --command="DELETE FROM jobs WHERE id = '$id'" >/dev/null 2>&1 && ((DELETED++)) || ((FAILED++))
    # Delete R2 objects
    CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}" \
        npx --prefix "$PROJECT_DIR/cloudflare/worker" wrangler r2 object delete "$R2_NAME/audio/$id" --force >/dev/null 2>&1 || true
    CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}" \
        npx --prefix "$PROJECT_DIR/cloudflare/worker" wrangler r2 object delete "$R2_NAME/results/$id/transcript.txt" --force >/dev/null 2>&1 || true
    CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}" \
        npx --prefix "$PROJECT_DIR/cloudflare/worker" wrangler r2 object delete "$R2_NAME/results/$id/segments.json" --force >/dev/null 2>&1 || true
done

echo ""
if [ "$FAILED" -gt 0 ]; then
    warn "Deleted $DELETED job(s), $FAILED failed."
else
    info "Deleted $DELETED job(s)."
fi
