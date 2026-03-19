#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
IMAGE="whisper-docker-transcribe"
TEST_FILE_URL="https://github.com/pyannote/pyannote-audio/raw/develop/tests/data/dev00.wav"
TEST_FILE="dev00.wav"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: .env not found at $ENV_FILE"
  exit 1
fi

HF_TOKEN=$(grep '^HF_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" | head -1)
if [ -z "$HF_TOKEN" ]; then
  echo "ERROR: HF_TOKEN not set in .env"
  echo "  1. Get a token at https://huggingface.co/settings/tokens"
  echo "  2. Accept the pyannote model license at https://huggingface.co/pyannote/speaker-diarization-3.1"
  echo "  3. Add HF_TOKEN=<your-token> to .env"
  exit 1
fi

if ! docker image inspect "$IMAGE" &>/dev/null; then
  echo "ERROR: Docker image '$IMAGE' not found. Run: docker build -t $IMAGE ."
  exit 1
fi

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "=== Speaker Diarization Test ==="
echo "Downloading test audio ($TEST_FILE, 2 speakers, 30s)..."
mkdir -p "$TMPDIR/input" "$TMPDIR/output"
curl -sL "$TEST_FILE_URL" -o "$TMPDIR/input/$TEST_FILE"

echo ""
echo "--- Test 1: Transcription WITH diarization ---"
chmod 777 "$TMPDIR/output"
docker run --rm --gpus all \
  -v "$TMPDIR/input:/input:ro" \
  -v "$TMPDIR/output:/output:rw" \
  -v "$HOME/.whisper-cache:/home/ubuntu/.cache:rw" \
  -e ANALYSIS=diarize \
  -e HF_TOKEN="$HF_TOKEN" \
  "$IMAGE" 2>&1

echo ""
echo "--- Checking output ---"
PASS=0
FAIL=0

if [ ! -f "$TMPDIR/output/segments.json" ]; then
  echo "FAIL: segments.json not found"
  FAIL=$((FAIL + 1))
else
  cp "$TMPDIR/output/segments.json" "$TMPDIR/segments_with.json"
  HAS_SPEAKERS=$(python3 -c "
import json
segs = json.load(open('$TMPDIR/segments_with.json'))
found = [s.get('speaker') for s in segs if s.get('speaker')]
print(len(found))
")
  if [ "$HAS_SPEAKERS" -gt 0 ]; then
    echo "PASS: Speaker labels found ($HAS_SPEAKERS segments with speakers)"
    PASS=$((PASS + 1))
    python3 -c "
import json
segs = json.load(open('$TMPDIR/segments_with.json'))
speakers = sorted(set(s.get('speaker') for s in segs if s.get('speaker')))
print(f'  Speakers: {speakers}')
for s in segs:
    label = s.get('speaker', '?')
    print(f'  [{s[\"start\"]:6.2f}-{s[\"end\"]:6.2f}] {label:12s} {s[\"text\"][:80]}')
"
  else
    echo "FAIL: No speaker labels in output"
    FAIL=$((FAIL + 1))
  fi
fi

echo ""
echo "--- Test 2: Transcription WITHOUT diarization (baseline) ---"
rm -rf "$TMPDIR/output"/*
chmod 777 "$TMPDIR/output"
docker run --rm --gpus all \
  -v "$TMPDIR/input:/input:ro" \
  -v "$TMPDIR/output:/output:rw" \
  -v "$HOME/.whisper-cache:/home/ubuntu/.cache:rw" \
  "$IMAGE" 2>&1 | grep -E '(Transcribing|Transcribe|Segments|Elapsed|Diariz)'

if [ ! -f "$TMPDIR/output/segments.json" ]; then
  echo "FAIL: segments.json not found"
  FAIL=$((FAIL + 1))
else
  cp "$TMPDIR/output/segments.json" "$TMPDIR/segments_without.json"
  HAS_SPEAKERS=$(python3 -c "
import json
segs = json.load(open('$TMPDIR/segments_without.json'))
found = [s.get('speaker') for s in segs if s.get('speaker')]
print(len(found))
")
  if [ "$HAS_SPEAKERS" -eq 0 ]; then
    echo "PASS: No speaker labels when diarization disabled"
    PASS=$((PASS + 1))
  else
    echo "FAIL: Unexpected speaker labels without diarization"
    FAIL=$((FAIL + 1))
  fi
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
