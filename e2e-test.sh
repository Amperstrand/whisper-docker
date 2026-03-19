#!/usr/bin/env bash
#
# e2e-test.sh — End-to-end transcription test against a deployed instance.
#
# Uploads a sample audio file, waits for transcription to complete,
# verifies the result contains valid segments, and cleans up.
#
# Usage:
#   ./e2e-test.sh                    # Test production (default)
#   ./e2e-test.sh https://custom.example.com  # Test custom URL
#
# No credentials required — all endpoints used by this test are public.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMPLE_FILE="$SCRIPT_DIR/tests/fixtures/sample.wav"
BASE_URL="${1:-https://listen.silent.energy}"
POLL_INTERVAL=5
MAX_WAIT=300  # 5 minutes — worker must be running and responsive

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass=0
fail=0

if [ ! -f "$SAMPLE_FILE" ]; then
    echo -e "${RED}FAIL${NC} Sample file not found: $SAMPLE_FILE"
    exit 1
fi

# No auth required — upload, status polling, result fetching, and cleanup
# are all public endpoints. Only GPU worker operations (claim, upload results,
# download audio) require authentication.

assert_eq() {
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
    if [ -z "$needle" ]; then
        if [ -n "$haystack" ]; then
            echo -e "  ${GREEN}PASS${NC} $name"
            pass=$((pass + 1))
        else
            echo -e "  ${RED}FAIL${NC} $name (value is empty)"
            fail=$((fail + 1))
        fi
    elif ! echo "$haystack" | grep -q "$needle"; then
        echo -e "  ${GREEN}PASS${NC} $name"
        pass=$((pass + 1))
    else
        echo -e "  ${RED}FAIL${NC} $name (should not contain: $needle)"
        fail=$((fail + 1))
    fi
}

assert_json_field() {
    local name="$1" json="$2" field="$3"
    local value
    value=$(echo "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field', 'MISSING'))" 2>/dev/null)
    if [ "$value" != "MISSING" ] && [ -n "$value" ]; then
        echo -e "  ${GREEN}PASS${NC} $name (value: $value)"
        pass=$((pass + 1))
    else
        echo -e "  ${RED}FAIL${NC} $name (field '$field' missing or empty)"
        fail=$((fail + 1))
    fi
}

echo -e "${YELLOW}=== E2E Transcription Test ===${NC}"
echo "  URL:    $BASE_URL"
echo "  Sample: $SAMPLE_FILE ($(du -h "$SAMPLE_FILE" | cut -f1))"
echo ""

# Step 1: Upload sample audio
echo "Step 1: Uploading sample audio..."
upload_resp=$(curl -s -X POST "$BASE_URL/api/jobs" \
    -F "file=@$SAMPLE_FILE")

job_id=$(echo "$upload_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
if [ -z "$job_id" ]; then
    echo -e "${RED}FAIL${NC} Upload failed: $upload_resp"
    fail=$((fail + 1))
else
    echo -e "  ${GREEN}PASS${NC} Upload returns job ID: $job_id"
fi

# Step 2: Wait for transcription to complete
echo ""
echo "Step 2: Waiting for transcription to complete (max ${MAX_WAIT}s)..."
elapsed=0
completed=false
status=""

while [ "$elapsed" -lt "$MAX_WAIT" ]; do
    job_resp=$(curl -s "$BASE_URL/api/jobs/$job_id")
    status=$(echo "$job_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job',{}).get('status',''))" 2>/dev/null)
    
    if [ "$status" = "completed" ]; then
        completed=true
        break
    fi
    
    if [ "$status" = "failed" ]; then
        echo -e "  ${RED}Job failed${NC}"
        error_msg=$(echo "$job_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job',{}).get('error_message',''))" 2>/dev/null)
        echo "  Error: $error_msg"
        fail=$((fail + 1))
        break
    fi
    
    echo "  Status: $status (${elapsed}s elapsed)..."
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
done

if [ "$completed" != "true" ]; then
    if [ "$elapsed" -ge "$MAX_WAIT" ]; then
        echo -e "  ${RED}FAIL${NC} Timed out after ${MAX_WAIT}s (status: $status)"
        fail=$((fail + 1))
    fi
fi

# Step 3: Fetch and verify result
echo ""
echo "Step 3: Fetching and verifying result..."
result_resp=$(curl -s "$BASE_URL/api/jobs/$job_id/result")

assert_json_field "Result has status" "$result_resp" "status"
assert_eq "Result status is completed" "$(echo "$result_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)" "completed"

segments=$(echo "$result_resp" | python3 -c "import sys,json; s=json.load(sys.stdin).get('segments'); print('yes' if s and len(s) > 0 else 'no')" 2>/dev/null)
assert_eq "Result has segments" "$segments" "yes"

segment_count=$(echo "$result_resp" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('segments',[])))" 2>/dev/null)
echo -e "  ${GREEN}PASS${NC} Segment count: $segment_count"

first_text=$(echo "$result_resp" | python3 -c "import sys,json; segs=json.load(sys.stdin).get('segments',[]); print(segs[0]['text'].strip() if segs else '')" 2>/dev/null)
assert_not_contains "Segments have text" "$first_text" ""

has_words=$(echo "$result_resp" | python3 -c "import sys,json; segs=json.load(sys.stdin).get('segments',[]); print('yes' if segs and segs[0].get('words') else 'no')" 2>/dev/null)
assert_eq "Segments have word timestamps" "$has_words" "yes"

word_count=$(echo "$result_resp" | python3 -c "import sys,json; segs=json.load(sys.stdin).get('segments',[]); print(sum(len(s.get('words',[])) for s in segs))" 2>/dev/null)
echo -e "  ${GREEN}PASS${NC} Total words: $word_count"

# Step 4: Verify transcript
transcript=$(echo "$result_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript','') or '')" 2>/dev/null)
assert_not_contains "Result has transcript" "$(echo "$transcript" | head -c 100)" ""

# Step 5: Cleanup — delete the test job
echo ""
echo "Step 4: Cleaning up test job..."
delete_status=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE_URL/api/jobs/$job_id")
assert_eq "Delete job succeeds" "$delete_status" "200"

# Summary
echo ""
total=$((pass + fail))
echo -e "${YELLOW}Results: $pass/$total passed, $fail failed${NC}"

if [ "$fail" -gt 0 ]; then
    exit 1
fi
