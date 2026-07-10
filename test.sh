#!/usr/bin/env bash
#
# test.sh — Comprehensive end-to-end test for whisper-docker.
#
# Usage:
#   ./test.sh          Quick smoke test (transcription + GPU check)
#   ./test.sh --full   Full test (all analysis stages, summarization, output formats)
#
# Requires: docker, nvidia-container-toolkit, espeak-ng (preferred) or espeak
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0

info()  { echo -e "${GREEN}[PASS]${NC} $*"; ((PASS++)) || true; }
warn()  { echo -e "${YELLOW}[SKIP]${NC} $*"; ((SKIP++)) || true; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; ((FAIL++)) || true; }
stage() { echo -e "\n${CYAN}--- $* ---${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FULL=false
[[ "${1:-}" == "--full" ]] && FULL=true

cleanup() {
    rm -f "$SCRIPT_DIR/input/test_speech.wav"
    docker compose -f "$SCRIPT_DIR/compose.yaml" down --remove-orphans 2>/dev/null || true
}
trap cleanup EXIT

COMPOSE="docker compose -f $SCRIPT_DIR/compose.yaml"

echo "============================================"
echo "  whisper-docker test suite"
echo "  Mode: $([ "$FULL" = true ] && echo 'full' || echo 'quick')"
echo "============================================"

# --- Stage 0: Prerequisites ---
stage "Prerequisites"
command -v docker >/dev/null 2>&1 || fail "docker not found"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin not found"
info "Docker available"

# --- Stage 1: GPU check ---
stage "GPU"
if ! command -v nvidia-smi >/dev/null 2>&1; then
    fail "nvidia-smi not found — GPU drivers not installed?"
elif ! nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
    fail "nvidia-smi cannot query GPU"
else
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1)
    info "GPU: $GPU_NAME ($GPU_MEM)"
fi

# --- Stage 2: Build ---
stage "Build"
BUILD_OUTPUT=$($COMPOSE build --quiet 2>&1)
BUILD_EXIT=$?
if [ $BUILD_EXIT -ne 0 ]; then
    fail "docker compose build failed (exit $BUILD_EXIT)"
    echo "$BUILD_OUTPUT"
else
    info "Docker image built"
fi

# --- Stage 3: Generate test audio ---
stage "Test audio"
TEST_AUDIO="$SCRIPT_DIR/input/test_speech.wav"

if ! command -v ffmpeg >/dev/null 2>&1; then
    fail "ffmpeg not found"
elif command -v espeak-ng >/dev/null 2>&1; then
    espeak-ng -w "$TEST_AUDIO" "Hello, this is a test of the transcription system." -s 150 2>/dev/null
    HAS_SPEECH=true
    info "Generated speech via espeak-ng ($(du -h "$TEST_AUDIO" | cut -f1))"
elif command -v espeak >/dev/null 2>&1; then
    espeak -w "$TEST_AUDIO" "Hello, this is a test of the transcription system." -s 150 2>/dev/null
    HAS_SPEECH=true
    info "Generated speech via espeak ($(du -h "$TEST_AUDIO" | cut -f1))"
else
    ffmpeg -f lavfi -i "sine=frequency=440:duration=3" -ar 16000 -ac 1 "$TEST_AUDIO" -y 2>/dev/null
    HAS_SPEECH=false
    warn "espeak-ng not found — using sine tone (transcript may be empty)"
fi
[ -f "$TEST_AUDIO" ] || fail "Failed to generate test audio"

# --- Stage 4: Quick mode ---
if [ "$FULL" = false ]; then
    stage "Transcription (quick)"

    OUTPUT_DIR="$SCRIPT_DIR/output"
    rm -rf "$OUTPUT_DIR"/*

    TRANSCRIBE_OUTPUT=$($COMPOSE up --build 2>&1)
    TRANSCRIBE_EXIT=$?

    if [ $TRANSCRIBE_EXIT -ne 0 ]; then
        fail "docker compose up exited with code $TRANSCRIBE_EXIT"
        echo "$TRANSCRIBE_OUTPUT" | tail -20
    else
        info "Container exited successfully"
    fi

    echo "$TRANSCRIBE_OUTPUT" | grep -qi "CUDA" || warn "No CUDA mention in output"

    [ -f "$OUTPUT_DIR/transcript.txt" ] || fail "output/transcript.txt missing"
    [ -f "$OUTPUT_DIR/segments.json" ] || fail "output/segments.json missing"
    info "Output files created"

    if [ "$HAS_SPEECH" = true ]; then
        TSIZE=$(stat -c%s "$OUTPUT_DIR/transcript.txt" 2>/dev/null || echo 0)
        SSIZE=$(stat -c%s "$OUTPUT_DIR/segments.json" 2>/dev/null || echo 0)
        [ "$TSIZE" -gt 0 ] || fail "transcript.txt is empty"
        [ "$SSIZE" -gt 0 ] || fail "segments.json is empty"
        info "transcript.txt non-empty ($TSIZE bytes)"
        info "segments.json non-empty ($SSIZE bytes)"
        echo ""
        echo "--- transcript.txt ---"
        cat "$OUTPUT_DIR/transcript.txt"
    fi

    echo ""
    echo "============================================"
    echo "  Results: $PASS passed, $FAIL failed, $SKIP skipped"
    if [ $FAIL -gt 0 ]; then
        echo "  STATUS: ${RED}FAILED${NC}"
    else
        echo "  STATUS: ${GREEN}PASSED${NC}"
    fi
    echo "============================================"
    [ $FAIL -eq 0 ] || exit 1
    exit 0
fi

# --- FULL MODE ---

# --- Stage 5: Full pipeline transcription ---
stage "Transcription (full pipeline)"
ANALYSIS_FLAGS="diarize,vad,emotion,classify,summarize"

OUTPUT_DIR="$SCRIPT_DIR/output"
rm -rf "$OUTPUT_DIR"/*

export ANALYSIS="$ANALYSIS_FLAGS"
export SUMMARY_BACKEND="hf"
export HF_TOKEN="${HF_TOKEN:-}"

TRANSCRIBE_OUTPUT=$($COMPOSE up --build 2>&1)
TRANSCRIBE_EXIT=$?

if [ $TRANSCRIBE_EXIT -ne 0 ]; then
    fail "Full pipeline exited with code $TRANSCRIBE_EXIT"
    echo "$TRANSCRIBE_OUTPUT" | tail -30
else
    info "Full pipeline completed"
fi

OUT="$OUTPUT_DIR"

# --- Stage 6: Verify all output files ---
stage "Output files"
for f in transcript.txt segments.json full.json analysis.json \
         transcript.srt transcript.vtt segments.csv words.csv \
         transcript.md report.md summary.txt summary.json summary.md; do
    if [ -f "$OUT/$f" ]; then
        SIZE=$(stat -c%s "$OUT/$f" 2>/dev/null || echo 0)
        info "$f ($SIZE bytes)"
    else
        fail "$f missing"
    fi
done

# --- Stage 7: Verify analysis.json fields ---
stage "Analysis content"
if [ -f "$OUT/analysis.json" ]; then
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
checks = [
    ('language', str),
    ('vad', dict),
    ('emotions', dict),
    ('audio_tags', list),
]
optional = [
    ('speakers', list),
]
for key, typ in checks:
    val = d.get(key)
    if val is None:
        print(f'MISSING: {key}')
        sys.exit(1)
    if not isinstance(val, typ):
        print(f'TYPE MISMATCH: {key} expected {typ.__name__}, got {type(val).__name__}')
        sys.exit(1)
for key, typ in optional:
    val = d.get(key)
    if val is not None and not isinstance(val, typ):
        print(f'TYPE MISMATCH: {key} expected {typ.__name__}, got {type(val).__name__}')
        sys.exit(1)
    if val is None:
        print(f'OPTIONAL_MISSING: {key} (synthetic audio)')
print('OK')
" "$OUT/analysis.json" 2>/dev/null && info "analysis.json has all expected fields" || fail "analysis.json missing fields"

    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
vad = d.get('vad', {})
if 'speech_ratio' not in vad:
    print('MISSING: vad.speech_ratio')
    sys.exit(1)
print(f'OK (speech_ratio={vad[\"speech_ratio\"]:.0%})')
" "$OUT/analysis.json" 2>/dev/null && info "VAD has speech_ratio" || fail "VAD missing speech_ratio"
else
    fail "analysis.json not found"
fi

# --- Stage 8: Verify summary ---
stage "Summarization (HF)"
if [ -f "$OUT/summary.json" ]; then
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
required = ['summary', 'key_points', 'topics', 'sentiment', 'confidence']
for key in required:
    if key not in d:
        print(f'MISSING: {key}')
        sys.exit(1)
print(f'OK (sentiment={d[\"sentiment\"]}, confidence={d[\"confidence\"]})')
" "$OUT/summary.json" 2>/dev/null && info "summary.json has all required keys" || fail "summary.json missing keys"
else
    fail "summary.json not found"
fi

# --- Stage 9: Verify word timestamps ---
stage "Word timestamps"
if [ -f "$OUT/words.csv" ]; then
    python3 -c "
import csv, sys
with open(sys.argv[1]) as f:
    reader = csv.DictReader(f)
    rows = list(reader)
if not rows:
    print('EMPTY: words.csv has no rows')
    sys.exit(1)
headers = rows[0].keys()
for h in ['word', 'start', 'end', 'probability']:
    if h not in headers:
        print(f'MISSING column: {h}')
        sys.exit(1)
print(f'OK ({len(rows)} words)')
" "$OUT/words.csv" 2>/dev/null && info "words.csv has word-level timestamps" || fail "words.csv missing required columns"
else
    fail "words.csv not found"
fi

# --- Stage 10: Transcript content ---
stage "Transcript content"
if [ "$HAS_SPEECH" = true ] && [ -f "$OUT/transcript.txt" ]; then
    TSIZE=$(stat -c%s "$OUT/transcript.txt" 2>/dev/null || echo 0)
    [ "$TSIZE" -gt 0 ] && info "transcript.txt non-empty ($TSIZE bytes)" || fail "transcript.txt is empty"
    echo ""
    echo "--- transcript.txt ---"
    cat "$OUT/transcript.txt"
elif [ -f "$OUT/transcript.txt" ]; then
    warn "Sine tone test — transcript may be empty (expected)"
fi

# --- Summary ---
echo ""
echo "============================================"
echo "  Results: $PASS passed, $FAIL failed, $SKIP skipped"
if [ $FAIL -gt 0 ]; then
    echo "  STATUS: ${RED}FAILED${NC}"
else
    echo "  STATUS: ${GREEN}ALL TESTS PASSED${NC}"
fi
echo "============================================"

[ $FAIL -eq 0 ] || exit 1
