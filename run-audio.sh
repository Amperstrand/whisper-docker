#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

: "${HF_TOKEN:?ERROR: HF_TOKEN not set in .env}"

INPUT_DIR="/home/ubuntu/Audio"
export STRATEGY="best-overall"
export GROUPED="true"
export PARALLEL_ANALYSIS="true"
export SUMMARY_SKIP_SYNTHESIS="true"

LOG="/tmp/whisper-audio-$(date +%Y%m%d-%H%M%S).log"
INCLUDE_FILE="/tmp/whisper-audio-include.txt"

echo "=== Audio transcription started at $(date) ===" | tee "$LOG"
echo "Strategy:    best-overall" | tee -a "$LOG"
echo "Log:         $LOG" | tee -a "$LOG"
echo "" | tee -a "$LOG"

echo "Running prefilter..." | tee -a "$LOG"
python3 -c "
import json, subprocess, os
r = subprocess.run(['python3', 'prefilter.py', '$INPUT_DIR', '--json'], capture_output=True, text=True)
files = json.loads(r.stdout)
ok = [f for f in files if f['status'] == 'ok' and f['file'].endswith('.wav')]
ok.sort(key=lambda x: os.path.getmtime(x['absolute_path']), reverse=True)
with open('$INCLUDE_FILE', 'w') as fp:
    for f in ok:
        fp.write(f['absolute_path'] + '\n')
total_dur = sum(f['duration_s'] for f in ok)
print(f'WAV files:       {len(ok)} (newest first)')
print(f'Total duration:  {total_dur:.0f}s ({total_dur/3600:.1f}h)')
" 2>&1 | tee -a "$LOG"

echo "" | tee -a "$LOG"

export INPUT_DIR="$INPUT_DIR"
export AUDIO_INCLUDE_FILE="$INCLUDE_FILE"

python3 batch.py "$INPUT_DIR" --strategy best-overall --force 2>&1 | tee -a "$LOG"

rm -f "$INCLUDE_FILE"

echo "" | tee -a "$LOG"
echo "=== Finished at $(date) ===" | tee -a "$LOG"
