#!/usr/bin/env bash
#
# prefetch.sh — Pre-download all models into persistent cache.
#
# Run once after initial setup. Models persist in ~/.cache/whisper-docker/
# and are reused by every subsequent transcription run.
#
# Usage:
#   ./prefetch.sh                  # Fetch all models
#   ./prefetch.sh --skip-hf-token  # Skip pyannote (no HF_TOKEN needed)
#   ./prefetch.sh --ollama         # Also pull the Ollama model
#   ./prefetch.sh --all            # Everything including Ollama
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SKIP_OLLAMA=true
SKIP_PYANNOTE=false

for arg in "${@:-}"; do
    case "$arg" in
        --skip-hf-token) SKIP_PYANNOTE=true ;;
        --ollama)  SKIP_OLLAMA=false ;;
        --all)     SKIP_OLLAMA=false ;;
    esac
done

CACHE_DIR="$HOME/.cache/whisper-docker"
mkdir -p "$CACHE_DIR"/{huggingface,torch,speechbrain}

echo "=== Model Prefetch ==="
echo "Cache dir: $CACHE_DIR"
echo ""

echo "Building Docker image..."
docker compose build --quiet 2>&1

DOCKER_ENV=()
if [ -n "${HF_TOKEN:-}" ] && [ "$SKIP_PYANNOTE" = false ]; then
    DOCKER_ENV+=(-e "HF_TOKEN=$HF_TOKEN")
    echo "HF_TOKEN set — will download pyannote diarization model"
else
    echo "HF_TOKEN not set — skipping pyannote (set HF_TOKEN=... to include it)"
fi

echo ""
echo "Downloading models into persistent cache..."
echo "(this may take a while on first run)"
echo ""

set +e
docker compose run --rm \
    "${DOCKER_ENV[@]}" \
    -v "$CACHE_DIR/huggingface:/home/ubuntu/.cache/huggingface" \
    -v "$CACHE_DIR/torch:/home/ubuntu/.cache/torch" \
    -v "$CACHE_DIR/speechbrain:/home/ubuntu/.cache/speechbrain" \
    transcribe python3 -c "
import gc
import os
import sys

errors = []

def try_download(name, fn):
    print(f'>>> {name}...')
    try:
        fn()
        import gc; gc.collect()
        print(f'    OK')
    except Exception as e:
        print(f'    FAILED: {e}')
        errors.append(name)

def dl_whisper():
    from faster_whisper import WhisperModel
    model = os.environ.get('WHISPER_MODEL', 'turbo')
    m = WhisperModel(model, device='cuda', compute_type='float16')
    del m
    gc.collect()

def dl_vad():
    import torch
    torch.hub.load('snakers4/silero-vad', 'silero_vad')
    if 'snakers4_silero-vad' in torch.hub._modules:
        del torch.hub._modules['snakers4_silero-vad']
    gc.collect()

def dl_pyannote():
    hf_token = os.environ.get('HF_TOKEN', '')
    if not hf_token:
        print('    Skipping (no HF_TOKEN)')
        return
    import torch
    _orig = torch.load
    torch.load = lambda *a, **kw: _orig(*a, **{**kw, 'weights_only': False})
    from pyannote.audio import Pipeline
    Pipeline.from_pretrained('pyannote/speaker-diarization-3.1', use_auth_token=hf_token)
    del Pipeline
    torch.cuda.empty_cache()
    gc.collect()

def dl_emotion():
    from transformers import pipeline
    p = pipeline('audio-classification', model='superb/wav2vec2-base-superb-er')
    del p
    gc.collect()

def dl_classify():
    from transformers import pipeline
    p = pipeline('audio-classification', model='MIT/ast-finetuned-audioset-10-10-0.4593')
    del p
    gc.collect()

def dl_lang_id():
    from speechbrain.inference.speaker import SpeakerRecognition
    lang_id = SpeakerRecognition.from_hparams(
        source='speechbrain/lang-id-commonlanguage_ecapa',
        savedir='/home/ubuntu/.cache/speechbrain/lang-id-ecapa',
    )
    del lang_id
    gc.collect()

def dl_summary_hf():
    import torch
    from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
    AutoTokenizer.from_pretrained('google/flan-t5-large')
    m = AutoModelForSeq2SeqLM.from_pretrained('google/flan-t5-large', torch_dtype=torch.float16, device_map='auto')
    del m
    torch.cuda.empty_cache()
    gc.collect()

try_download('Whisper (${WHISPER_MODEL:-turbo})', dl_whisper)
try_download('Silero VAD', dl_vad)
try_download('Pyannote diarization', dl_pyannote)
try_download('Emotion (wav2vec2)', dl_emotion)
try_download('Audio classification (AST)', dl_classify)
try_download('Language ID (SpeechBrain)', dl_lang_id)
try_download('Summary (HF: flan-t5-large)', dl_summary_hf)

print()
if errors:
    print(f'Completed with {len(errors)} error(s):')
    for e in errors:
        print(f'  - {e}')
    sys.exit(1)
else:
    print('=== All models cached successfully ===')
" 2>&1
EXIT_CODE=$?
set -e

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "Some models failed to download. Transcription will still work,"
    echo "but the failed modules will be skipped at runtime."
fi

if [ "$SKIP_OLLAMA" = false ]; then
    echo ""
    echo "=== Pulling Ollama model ==="
    docker compose --profile summarize up -d ollama
    echo "Waiting for Ollama..."
    for i in $(seq 1 30); do
        if docker exec whisper-ollama curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo "  Ready."
            break
        fi
        echo "  waiting... ($i/30)"
        sleep 2
    done
    docker exec whisper-ollama ollama pull llama3.2:3b
    docker compose --profile summarize stop ollama
    echo "=== Ollama model cached ==="
fi

echo ""
echo "Cache contents:"
du -sh "$CACHE_DIR"/* 2>/dev/null || echo "  (empty)"
echo ""
echo "Models are cached at: $CACHE_DIR/"
echo "They will persist across runs. To clear:"
echo "  rm -rf $CACHE_DIR/"
