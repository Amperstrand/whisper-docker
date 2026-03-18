# whisper-docker

Local GPU audio transcription in one command. Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) with NVIDIA CUDA acceleration inside Docker.

**No Python on the host. No virtualenvs. No model management. Just `docker compose up`.**

## Quick Start

**Prerequisites:** NVIDIA GPU with driver installed, Docker Engine + Compose plugin, NVIDIA Container Toolkit.

```bash
git clone https://github.com/Amperstrand/whisper-docker.git
cd whisper-docker
cp /path/to/your/audio.mp3 input/
docker compose up --build
```

Results appear in `output/`:
- `transcript.txt` — plain text, one line per segment
- `segments.json` — structured JSON with timestamps and word-level data

## Fresh Ubuntu Setup

If you have a fresh Ubuntu 22.04 or 24.04 machine with an NVIDIA GPU driver already installed:

```bash
git clone https://github.com/Amperstrand/whisper-docker.git
cd whisper-docker
sudo ./setup.sh
# Log out and back in for docker group (or: newgrp docker)
docker compose up --build
```

`setup.sh` installs from official repositories only:
- Docker Engine + Compose plugin (from `download.docker.com`)
- NVIDIA Container Toolkit (from `nvidia.github.io`)
- Validates GPU passthrough end-to-end

Idempotent — safe to run multiple times.

## Prerequisites

| Requirement | How to check |
|---|---|
| NVIDIA GPU + driver | `nvidia-smi` shows your GPU |
| Docker Engine | `docker --version` |
| Docker Compose plugin | `docker compose version` |
| NVIDIA Container Toolkit | `nvidia-ctk --version` |

If anything is missing, run `sudo ./setup.sh` or install manually following the [Docker](https://docs.docker.com/engine/install/ubuntu/) and [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) docs.

## Supported Audio Formats

`.wav` `.mp3` `.m4a` `.flac` `.ogg`

Drop any supported file into `input/`. Only the first file found is transcribed.

## Output

### transcript.txt

```
Hello, this is a test of the local transcription system.
The quick brown fox jumps over the lazy dog.
GPU acceleration makes this fast and efficient.
```

### segments.json

```json
[
  {
    "start": 0.0,
    "end": 3.52,
    "text": "Hello, this is a test of the local transcription system.",
    "words": [
      { "word": " Hello,", "start": 0.0, "end": 0.36, "probability": 0.9053 },
      ...
    ]
  },
  ...
]
```

## Changing the Model

Edit `transcribe.py` and change the model name in the `WhisperModel(...)` call:

```python
model = WhisperModel("turbo", device="cuda", compute_type="float16")
```

Available models (size vs. accuracy tradeoff):

| Model | VRAM | Speed | Accuracy |
|---|---|---|---|
| `tiny` | ~1 GB | Fastest | Lowest |
| `base` | ~1 GB | Very fast | Low |
| `small` | ~2 GB | Fast | Medium |
| `medium` | ~5 GB | Moderate | Good |
| `large-v3` | ~10 GB | Slow | Best |
| `turbo` | ~5 GB | Fast | Good |

## How It Works

1. Docker image: `nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04` (CUDA 12.6 + cuDNN 9)
2. faster-whisper uses CTranslate2 with pre-built CUDA 12 wheels
3. On first run, the model is downloaded from Hugging Face and cached
4. Subsequent runs reuse the cached model (fast startup)

## License

[MIT](LICENSE)
