# whisper-docker

Local GPU audio transcription, or a full cloud transcription service — your choice.

Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) with NVIDIA CUDA acceleration inside Docker.

## Two Modes

### Local Mode

Transcribe audio files on your machine. No cloud, no accounts, no API keys.

```bash
git clone https://github.com/Amperstrand/whisper-docker.git
cd whisper-docker
cp /path/to/your/audio.mp3 input/
docker compose up --build
```

Results appear in `output/`:
- `transcript.txt` — plain text, one line per segment
- `segments.json` — structured JSON with timestamps and word-level data

### Cloud Mode

Deploy a Cloudflare Worker (API + frontend) and connect a GPU worker agent for on-demand transcription from anywhere.

```
Browser → Cloudflare Worker (API + UI) → R2 (storage) + D1 (queue)
                                            ↕
                                      GPU Worker (your machine)
```

```bash
# 1. Deploy to Cloudflare
./deploy.sh

# 2. Start GPU worker
cd gpu-worker && cp config.example.env .env
# Edit .env with your API URL and token
docker compose -f compose.worker.yaml up --build
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full instructions.

## Quick Test (Local)

```bash
git clone https://github.com/Amperstrand/whisper-docker.git
cd whisper-docker
sudo ./setup.sh  # if needed
./test.sh
```

## Fresh Ubuntu Setup

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

## Cloud Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      CLOUDFLARE EDGE                         │
│                                                              │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │ Frontend │───▶│  API Worker  │───▶│  R2 + D1         │  │
│  │ (served  │    │              │    │                  │  │
│  │  by      │    │ POST /jobs   │    │  R2: audio/      │  │
│  │  Worker) │    │ GET  /jobs   │    │      results/    │  │
│  └──────────┘    └──────┬───────┘    │                  │  │
│                         │            │  D1: jobs table  │  │
└─────────────────────────┼────────────┴──────────────────┘  │
                          │ HTTPS (polling)                    │
                          ▼                                    │
┌──────────────────────────────────────────────────────────────┐
│                   GPU WORKER (your machine)                  │
│                                                              │
│  Polls for jobs → downloads audio → transcribes → uploads    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Project Structure

```
whisper-docker/
├── Dockerfile              # Local transcription container
├── compose.yaml            # Local Docker Compose
├── transcribe.py           # Transcription script
├── setup.sh                # Host setup (Docker + NVIDIA)
├── test.sh                 # End-to-end test
├── deploy.sh               # Cloudflare deployment script
├── cloudflare/
│   ├── worker/
│   │   ├── src/            # Cloudflare Worker (TypeScript)
│   │   ├── frontend/       # Web UI (HTML/CSS/JS)
│   │   └── wrangler.toml   # Worker configuration
│   └── schema.sql          # D1 database schema
├── gpu-worker/
│   ├── worker.py           # GPU worker agent
│   ├── Dockerfile.worker   # Worker container
│   └── compose.worker.yaml # Worker Docker Compose
├── scripts/
│   └── cleanup.sh          # Manual job cleanup
└── docs/
    ├── DEPLOYMENT.md       # Deployment guide
    ├── API.md              # API reference
    └── WORKER.md           # GPU worker guide
```

## Supported Audio Formats

`.wav` `.mp3` `.m4a` `.flac` `.ogg` `.webm`

Maximum file size: 100 MB

## Prerequisites (Local)

| Requirement | How to check |
|---|---|
| NVIDIA GPU + driver | `nvidia-smi` shows your GPU |
| Docker Engine | `docker --version` |
| Docker Compose plugin | `docker compose version` |
| NVIDIA Container Toolkit | `nvidia-ctk --version` |

## Prerequisites (Cloud)

| Requirement | How to check |
|---|---|
| Cloudflare account | `npx wrangler whoami` |
| Node.js 18+ | `node --version` |
| GPU worker prerequisites | See above |

## Changing the Model

Edit `transcribe.py` (local) or set `WHISPER_MODEL` env var (cloud):

| Model | VRAM | Speed | Accuracy |
|---|---|---|---|
| `tiny` | ~1 GB | Fastest | Lowest |
| `base` | ~1 GB | Very fast | Low |
| `small` | ~2 GB | Fast | Medium |
| `medium` | ~5 GB | Moderate | Good |
| `large-v3` | ~10 GB | Slow | Best |
| `turbo` | ~5 GB | Fast | Good |

## Documentation

- [Deployment Guide](docs/DEPLOYMENT.md) — Full Cloudflare setup
- [API Reference](docs/API.md) — All endpoints with examples
- [Worker Guide](docs/WORKER.md) — GPU worker configuration

## License

[MIT](LICENSE)
