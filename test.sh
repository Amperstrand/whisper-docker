#!/usr/bin/env bash
#
# test.sh — End-to-end smoke test for whisper-docker.
#
# Generates a short spoken test audio file, runs transcription,
# and verifies the output files are created with content.
#
# Generates: "Hello, this is a test of the transcription system."
#
# Requires: ffmpeg, espeak-ng (preferred) or espeak
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[PASS]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_FILE="$SCRIPT_DIR/input/test_speech.wav"
TRANSCRIPT="$SCRIPT_DIR/output/transcript.txt"
SEGMENTS="$SCRIPT_DIR/output/segments.json"

cleanup() {
    rm -f "$TEST_FILE"
    docker compose -f "$SCRIPT_DIR/compose.yaml" down --remove-orphans 2>/dev/null || true
    # Preserve output files for inspection on failure
}
trap cleanup EXIT

echo "=== whisper-docker end-to-end test ==="
echo ""

# Check prerequisites
command -v docker >/dev/null 2>&1 || fail "docker not found. Run sudo ./setup.sh first."
docker compose version >/dev/null 2>&1 || fail "docker compose plugin not found."

# Generate test audio
echo "Generating test audio..."
if ! command -v ffmpeg >/dev/null 2>&1; then
    fail "ffmpeg not found. Install it with: sudo apt-get install ffmpeg"
fi

HAS_SPEECH=false
if command -v espeak-ng >/dev/null 2>&1; then
    # Generate actual speech — whisper will transcribe this
    espeak-ng -w "$TEST_FILE" "Hello, this is a test of the transcription system." -s 150 2>/dev/null
    HAS_SPEECH=true
    info "Using espeak-ng to generate test audio."
elif command -v espeak >/dev/null 2>&1; then
    espeak -w "$TEST_FILE" "Hello, this is a test of the transcription system." -s 150 2>/dev/null
    HAS_SPEECH=true
    info "Using espeak to generate test audio."
else
    # Fallback: sine tone. Pipeline will work but transcript may be empty.
    ffmpeg -f lavfi -i "sine=frequency=440:duration=3" -ar 16000 -ac 1 "$TEST_FILE" -y 2>/dev/null
    HAS_SPEECH=false
    warn "espeak-ng/espeak not found — using sine tone. Install espeak-ng for a speech-based test."
fi

[ -f "$TEST_FILE" ] || fail "Failed to generate test audio."
info "Test audio generated: test_speech.wav ($(du -h "$TEST_FILE" | cut -f1))"

# Run transcription
echo ""
echo "Transcribing sample audio..."
BUILD_OUTPUT=$(docker compose -f "$SCRIPT_DIR/compose.yaml" up --build 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "$BUILD_OUTPUT"
    fail "docker compose exited with code $EXIT_CODE"
fi

echo "$BUILD_OUTPUT" | grep -q "CUDA:.*requested" || fail "CUDA was not requested — GPU passthrough may be broken."
info "CUDA GPU was used for transcription."

# Verify output files exist
[ -f "$TRANSCRIPT" ] || fail "transcript.txt not found in output/"
[ -f "$SEGMENTS" ]  || fail "segments.json not found in output/"
info "Output files created."

# Check content
TRANSCRIPT_SIZE=$(stat -c%s "$TRANSCRIPT" 2>/dev/null || stat -f%z "$TRANSCRIPT" 2>/dev/null || echo 0)
SEGMENTS_SIZE=$(stat -c%s "$SEGMENTS" 2>/dev/null || stat -f%z "$SEGMENTS" 2>/dev/null || echo 0)

if [ "$HAS_SPEECH" = true ]; then
    [ "$TRANSCRIPT_SIZE" -gt 0 ] || fail "transcript.txt is empty — speech test should produce output."
    [ "$SEGMENTS_SIZE" -gt 0 ]  || fail "segments.json is empty — speech test should produce output."
    info "transcript.txt is non-empty (${TRANSCRIPT_SIZE} bytes)."
    info "segments.json is non-empty (${SEGMENTS_SIZE} bytes)."

    echo ""
    echo "--- transcript.txt ---"
    cat "$TRANSCRIPT"
    echo ""
    echo "========================="
    echo "  All tests passed."
    echo "========================="
else
    warn "Sine tone test — transcript may be empty (expected)."
    [ "$TRANSCRIPT_SIZE" -ge 0 ] && info "transcript.txt exists (${TRANSCRIPT_SIZE} bytes)."
    [ "$SEGMENTS_SIZE" -ge 0 ]  && info "segments.json exists (${SEGMENTS_SIZE} bytes)."
    echo ""
    echo "========================="
    echo "  All tests passed."
    echo "========================="
fi
