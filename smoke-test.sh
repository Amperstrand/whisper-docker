#!/usr/bin/env bash
#
# smoke-test.sh — Automated smoke tests for deployed Whisper Transcribe.
#
# Usage:
#   ./smoke-test.sh              # Test both beta and production
#   ./smoke-test.sh production   # Test production only
#   ./smoke-test.sh beta         # Test beta only
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

PROD_URL="${PROD_URL:-https://listen.silent.energy}"
BETA_URL="${BETA_URL:-https://beta.listen.silent.energy}"

TARGET="${1:-all}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass=0
fail=0
skip=0

assert() {
    local name="$1" actual="$2" expected="$3"
    if [ "$actual" = "$expected" ]; then
        echo -e "  ${GREEN}PASS${NC} $name"
        pass=$((pass + 1))
    else
        echo -e "  ${RED}FAIL${NC} $name (expected: $expected, got: $actual)"
        fail=$((fail + 1))
    fi
}

assert_contains() {
    local name="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -q "$needle"; then
        echo -e "  ${GREEN}PASS${NC} $name"
        pass=$((pass + 1))
    else
        echo -e "  ${RED}FAIL${NC} $name (missing: $needle)"
        fail=$((fail + 1))
    fi
}

assert_not_contains() {
    local name="$1" haystack="$2" needle="$3"
    if ! echo "$haystack" | grep -q "$needle"; then
        echo -e "  ${GREEN}PASS${NC} $name"
        pass=$((pass + 1))
    else
        echo -e "  ${RED}FAIL${NC} $name (should not contain: $needle)"
        fail=$((fail + 1))
    fi
}

test_site() {
    local url="$1"
    local label="$2"

    echo ""
    echo -e "${YELLOW}=== $label ($url) ===${NC}"

    local html
    html=$(curl -s "$url/")
    local headers
    headers=$(curl -s -I "$url/")

    assert "HTML returns 200" "$(curl -s -o /dev/null -w '%{http_code}' "$url/")" "200"
    assert_contains "Drag/drop prevention script" "$html" 'dragover.*preventDefault'
    assert_not_contains "SSR disabled (no drop-zone in HTML)" "$html" 'drop-zone'
    assert_contains "Cache-Control: no-cache on HTML" "$headers" 'cache-control: no-cache'

    local start_js
    start_js=$(echo "$html" | grep -oP '_app/immutable/entry/start\.\w+\.js' | head -1)
    if [ -n "$start_js" ]; then
        assert "JS bundle exists" "$(curl -s -o /dev/null -w '%{http_code}' "$url/$start_js")" "200"
    else
        echo -e "  ${RED}FAIL${NC} JS bundle not found in HTML"
        fail=$((fail + 1))
    fi

    local app_js
    app_js=$(echo "$html" | grep -oP '_app/immutable/entry/app\.[\w-]+\.js' | head -1)
    if [ -n "$app_js" ]; then
        local app_content
        app_content=$(curl -s "$url/$app_js")
        assert_contains "CSS assets referenced in app bundle" "$app_content" ".css"
    else
        echo -e "  ${RED}FAIL${NC} App JS bundle not found in HTML"
        fail=$((fail + 1))
    fi

    assert "Health endpoint ok" "$(curl -s "$url/health" | grep -o '"status":"ok"')" '"status":"ok"'
    assert "API health endpoint ok" "$(curl -s "$url/api/health" | grep -o '"status":"ok"')" '"status":"ok"'

    assert "GET fake job returns 404" "$(curl -s -o /dev/null -w '%{http_code}' "$url/api/jobs/00000000-0000-0000-0000-000000000000")" "404"
    assert "GET fake result returns 404" "$(curl -s -o /dev/null -w '%{http_code}' "$url/api/jobs/00000000-0000-0000-0000-000000000000/result")" "404"
    assert "GET audio without auth returns 401" "$(curl -s -o /dev/null -w '%{http_code}' "$url/api/jobs/00000000-0000-0000-0000-000000000000/audio")" "401"
    assert "POST jobs without file returns 400" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$url/api/jobs")" "400"

    assert "GET jobs without auth returns 401" "$(curl -s -o /dev/null -w '%{http_code}' "$url/api/jobs?status=pending")" "401"
}

if [ "$TARGET" = "all" ] || [ "$TARGET" = "production" ]; then
    test_site "$PROD_URL" "Production"
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "beta" ]; then
    test_site "$BETA_URL" "Beta"
fi

echo ""
total=$((pass + fail))
echo "Results: $pass/$total passed, $fail failed"

if [ "$fail" -gt 0 ]; then
    exit 1
fi
