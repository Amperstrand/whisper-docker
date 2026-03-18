#!/usr/bin/env bash
#
# cleanup.sh — Remove old jobs and their R2 storage.
#
# Usage:
#   ./scripts/cleanup.sh [DAYS]
#
#   DAYS: Delete jobs older than this many days (default: 30)
#
# Requires: wrangler authenticated, .env with CLOUDFLARE_ACCOUNT_ID
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
ENV_FILE="$PROJECT_DIR/.env"
DAYS="${1:-30}"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env file not found. Run deploy.sh first." >&2
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

D1_NAME="${D1_DATABASE_NAME:-whisper-transcriptions}"

echo "Finding jobs older than $DAYS days..."

ROWS=$(npx wrangler d1 execute "$D1_NAME" --remote \
    --command="SELECT id, status, original_filename FROM jobs WHERE created_at < datetime('now', '-${DAYS} days') ORDER BY created_at ASC" \
    --json 2>/dev/null || echo "[]")

COUNT=$(echo "$ROWS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)[0].get('results',[])))" 2>/dev/null || echo "0")

if [ "$COUNT" = "0" ]; then
    echo "No jobs older than $DAYS days found."
    exit 0
fi

echo "Found $COUNT job(s) to delete."
read -rp "Proceed? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Cancelled."
    exit 0
fi

IDS=$(echo "$ROWS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for row in data[0].get('results', []):
    print(row['id'])
" 2>/dev/null)

for id in $IDS; do
    echo "Deleting job $id..."
    # Delete from D1
    npx wrangler d1 execute "$D1_NAME" --remote \
        --command="DELETE FROM jobs WHERE id = '$id'" >/dev/null 2>&1 || true
    # Delete R2 objects (audio + results)
    npx wrangler r2 object delete "whisper-transcriptions/audio/$id" --force >/dev/null 2>&1 || true
    npx wrangler r2 object delete "whisper-transcriptions/results/$id/transcript.txt" --force >/dev/null 2>&1 || true
    npx wrangler r2 object delete "whisper-transcriptions/results/$id/segments.json" --force >/dev/null 2>&1 || true
done

echo "Done. Deleted $COUNT job(s)."
