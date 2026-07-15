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

export INPUT_DIR="/home/ubuntu/mp3"
export GROUPED="true"
export PARALLEL_ANALYSIS="true"
export SUMMARY_SKIP_SYNTHESIS="true"

LOG="/tmp/whisper-compare-$(date +%Y%m%d-%H%M%S).log"

echo "=== Compare run started at $(date) ===" | tee "$LOG"
echo "Strategies:  best-overall, norwegian" | tee -a "$LOG"
echo "Log:         $LOG" | tee -a "$LOG"
echo "" | tee -a "$LOG"

python3 batch.py "$INPUT_DIR" --compare best-overall,norwegian --force 2>&1 | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "=== Finished at $(date) ===" | tee -a "$LOG"
