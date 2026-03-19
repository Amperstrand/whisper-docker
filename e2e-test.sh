#!/usr/bin/env bash
#
# e2e-test.sh — End-to-end transcription test against a deployed instance.
#
# Downloads a sample audio file, uploads it, waits for transcription to
# complete, verifies the result, and cleans up.
#
# Usage:
#   ./e2e-test.sh                    # Test production (default)
#   ./e2e-test.sh production         # Test production
#   ./e2e-test.sh beta               # Test beta
#   ./e2e-test.sh https://custom.example.com  # Test custom URL
#
# No credentials required — all endpoints used are public.
#
set -euo pipefail

case "${1:-}" in
    production) BASE_URL="https://listen.silent.energy" ;;
    beta)        BASE_URL="https://beta.listen.silent.energy" ;;
    https://*)   BASE_URL="$1" ;;
    *)           BASE_URL="https://listen.silent.energy" ;;
esac

SAMPLE_URL="https://github.com/openai/whisper/raw/main/tests/jfk.flac"
SAMPLE_FILE="/tmp/e2e-test-sample.flac"
POLL_INTERVAL=5
MAX_WAIT=300

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass=0
fail=0

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

assert_not_empty() {
    local name="$1" value="$2"
    if [ -n "$value" ]; then
        echo -e "  ${GREEN}PASS${NC} $name"
        pass=$((pass + 1))
    else
        echo -e "  ${RED}FAIL${NC} $name (value is empty)"
        fail=$((fail + 1))
    fi
}

echo -e "${YELLOW}=== E2E Transcription Test ===${NC}"
echo "  URL: $BASE_URL"

# Step 0: Download sample audio
echo "Step 0: Downloading sample audio..."
if [ -f "$SAMPLE_FILE" ]; then
    echo -e "  ${GREEN}PASS${NC} Using cached sample: $SAMPLE_FILE"
else
    dl_status=$(curl -sL -w '%{http_code}' -o "$SAMPLE_FILE" "$SAMPLE_URL")
    if [ "$dl_status" != "200" ]; then
        echo -e "${RED}FAIL${NC} Download failed (HTTP $dl_status) from $SAMPLE_URL"
        exit 1
    fi
    echo -e "  ${GREEN}PASS${NC} Downloaded sample: $SAMPLE_FILE ($(du -h "$SAMPLE_FILE" | cut -f1))"
fi
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

status_val=$(echo "$result_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
assert_eq "Result status is completed" "$status_val" "completed"

segment_count=$(echo "$result_resp" | python3 -c "import sys,json; segs=json.load(sys.stdin).get('segments',[]); print(len(segs))" 2>/dev/null)
assert_not_empty "Result has segments" "$segment_count"
echo -e "  ${GREEN}PASS${NC} Segment count: $segment_count"

first_text=$(echo "$result_resp" | python3 -c "import sys,json; segs=json.load(sys.stdin).get('segments',[]); print((segs[0]['text'].strip() if segs else '').strip())" 2>/dev/null)
assert_not_empty "Segments have text" "$first_text"

has_words=$(echo "$result_resp" | python3 -c "import sys,json; segs=json.load(sys.stdin).get('segments',[]); print('yes' if segs and segs[0].get('words') else 'no')" 2>/dev/null)
assert_eq "Segments have word timestamps" "$has_words" "yes"

word_count=$(echo "$result_resp" | python3 -c "import sys,json; segs=json.load(sys.stdin).get('segments',[]); print(sum(len(s.get('words',[])) for s in segs))" 2>/dev/null)
echo -e "  ${GREEN}PASS${NC} Total words: $word_count"

transcript=$(echo "$result_resp" | python3 -c "import sys,json; print((json.load(sys.stdin).get('transcript','') or '').strip())" 2>/dev/null)
assert_not_empty "Result has transcript" "$transcript"

# Step 4: Cleanup — delete the test job
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
