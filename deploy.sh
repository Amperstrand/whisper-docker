#!/usr/bin/env bash
#
# deploy.sh — Deploy Whisper Transcribe to Cloudflare.
#
# Prerequisites:
#   - Node.js 18+ and npm
#   - wrangler authenticated (wrangler login)
#   - .env file configured (see below)
#
# Usage:
#   ./deploy.sh              # Deploy to beta (beta.listen.silent.energy)
#   ./deploy.sh production   # Deploy to production (listen.silent.energy)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
WORKER_DIR="$SCRIPT_DIR/cloudflare/worker"

DEPLOY_ENV="${1:-beta}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[SKIP]${NC} $*"; }
step()  { echo -e "${BLUE}[...]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

if [ "$DEPLOY_ENV" != "beta" ] && [ "$DEPLOY_ENV" != "production" ]; then
    fail "Usage: $0 [beta|production]"
fi

WRANGLER_ENV_FLAG=""
if [ "$DEPLOY_ENV" = "production" ]; then
    WRANGLER_ENV_FLAG="--env production"
    echo "Deploying to PRODUCTION (listen.silent.energy)"
else
    echo "Deploying to BETA (beta.listen.silent.energy)"
fi

# ---------------------------------------------------------------------------
# 1. Load .env
# ---------------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
    echo "No .env file found. Creating one from template..."
    cat > "$ENV_FILE" <<'TEMPLATE'
# Cloudflare account ID (from dashboard or: wrangler whoami)
CLOUDFLARE_ACCOUNT_ID=

# R2 bucket name for audio/results storage
R2_BUCKET_NAME=whisper-transcriptions

# D1 database name
D1_DATABASE_NAME=whisper-transcriptions

# Bearer token for GPU worker authentication
# Generate with: openssl rand -hex 32
WORKER_TOKEN=

# CORS origin (use * for open access, or your domain)
CORS_ORIGIN=*
TEMPLATE
    warn "Created .env — fill in CLOUDFLARE_ACCOUNT_ID and WORKER_TOKEN, then re-run deploy.sh"
    exit 0
fi

set -a
source "$ENV_FILE"
set +a

if [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
    fail "CLOUDFLARE_ACCOUNT_ID is not set in .env. Get it from https://dash.cloudflare.com or run 'wrangler whoami'."
fi

# ---------------------------------------------------------------------------
# 2. Check prerequisites
# ---------------------------------------------------------------------------
step "Checking prerequisites..."

command -v node >/dev/null 2>&1 || fail "Node.js not found. Install from https://nodejs.org"
command -v npx >/dev/null 2>&1 || fail "npx not found (install Node.js)"
command -v wrangler >/dev/null 2>&1 || fail "wrangler not found. Run: npm install -g wrangler"

if ! npx wrangler whoami 2>&1 | grep -q "Account"; then
    fail "wrangler is not authenticated. Run: npx wrangler login"
fi
info "Prerequisites OK"

# ---------------------------------------------------------------------------
# 3. Generate WORKER_TOKEN if not set
# ---------------------------------------------------------------------------
if [ -z "${WORKER_TOKEN:-}" ]; then
    WORKER_TOKEN=$(openssl rand -hex 32)
    echo "WORKER_TOKEN=$WORKER_TOKEN" >> "$ENV_FILE"
    warn "Generated WORKER_TOKEN and saved to .env"
fi

# ---------------------------------------------------------------------------
# 4. Create R2 bucket
# ---------------------------------------------------------------------------
step "Setting up R2 bucket..."
if npx wrangler r2 bucket list 2>&1 | grep -q "${R2_BUCKET_NAME:-whisper-transcriptions}"; then
    warn "R2 bucket '${R2_BUCKET_NAME:-whisper-transcriptions}' already exists"
else
    npx wrangler r2 bucket create "${R2_BUCKET_NAME:-whisper-transcriptions}" 2>&1 || fail "Failed to create R2 bucket"
    info "R2 bucket created"
fi

# ---------------------------------------------------------------------------
# 5. Create D1 database
# ---------------------------------------------------------------------------
step "Setting up D1 database..."
D1_NAME="${D1_DATABASE_NAME:-whisper-transcriptions}"

DB_ID=$(npx wrangler d1 list 2>&1 | grep -A1 "$D1_NAME" | grep -oP '^\w+\s+\K[a-f0-9]{32}' | head -1 || true)

if [ -z "$DB_ID" ]; then
    CREATE_OUTPUT=$(npx wrangler d1 create "$D1_NAME" 2>&1)
    DB_ID=$(echo "$CREATE_OUTPUT" | grep -oP 'database_id\s*=\s*"\K[a-f0-9]+')
    if [ -z "$DB_ID" ]; then
        fail "Failed to create D1 database or parse database_id"
    fi
    info "D1 database created (id: $DB_ID)"
else
    info "D1 database exists (id: $DB_ID)"
fi

# ---------------------------------------------------------------------------
# 6. Apply schema
# ---------------------------------------------------------------------------
step "Applying database schema..."
npx wrangler d1 execute "$D1_NAME" --remote --file="$SCRIPT_DIR/cloudflare/schema.sql" 2>&1 || fail "Failed to apply schema"
info "Schema applied"

# ---------------------------------------------------------------------------
# 7. Build SvelteKit and deploy
# ---------------------------------------------------------------------------
step "Installing Worker dependencies..."
cd "$WORKER_DIR"
npm install 2>&1 || fail "npm install failed"
info "Dependencies installed"

step "Building SvelteKit..."
# shellcheck disable=SC2086
npx wrangler deploy $WRANGLER_ENV_FLAG 2>&1 || fail "Worker deployment failed"

# ---------------------------------------------------------------------------
# 8. Set WORKER_TOKEN as secret
# ---------------------------------------------------------------------------
step "Setting Worker secrets..."
# shellcheck disable=SC2086
echo "$WORKER_TOKEN" | npx wrangler secret put WORKER_TOKEN $WRANGLER_ENV_FLAG 2>&1 || fail "Failed to set WORKER_TOKEN secret"
info "WORKER_TOKEN secret set"

cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 9. Done
# ---------------------------------------------------------------------------
if [ "$DEPLOY_ENV" = "production" ]; then
    WORKER_URL="https://listen.silent.energy"
else
    WORKER_URL="https://beta.listen.silent.energy"
fi

echo ""
echo "=========================================="
echo "  Deployment complete ($DEPLOY_ENV)!"
echo "=========================================="
echo ""
echo "  Worker URL:  $WORKER_URL"
echo "  Health:      $WORKER_URL/health"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Verify health check:"
echo "     curl $WORKER_URL/health"
echo ""
echo "  2. Configure GPU worker:"
echo "     Edit .env — set API_URL=$WORKER_URL and WORKER_TOKEN=$WORKER_TOKEN"
echo ""
echo "  3. Upload audio and test:"
echo "     Open $WORKER_URL in your browser"
echo ""
