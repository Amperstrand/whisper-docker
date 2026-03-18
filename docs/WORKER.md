# GPU Worker Setup Guide

The GPU worker polls the Cloudflare Worker API for pending transcription jobs, downloads audio, runs faster-whisper on your GPU, and uploads results.

## Prerequisites

- NVIDIA GPU with driver installed
- Docker Engine + Compose plugin + NVIDIA Container Toolkit
- Python 3.10+ (ships with Ubuntu, or for direct mode)

Run `sudo ./setup.sh` from the project root if you need to install Docker and NVIDIA toolkit.

## Quick Start

### 1. Configure

All config lives in the project root `.env` file (gitignored). Make sure these variables are set:

```env
API_URL=https://listen.silent.energy
WORKER_TOKEN=<same token from project root .env>
```

Optional variables (with defaults):

```env
WORKER_ID=gpu-1          # auto-generated if not set
POLL_INTERVAL=60         # seconds between polls
MAX_CONCURRENT_JOBS=1    # parallel jobs (requires sufficient VRAM)
MODE=docker              # docker or direct
WHISPER_MODEL=turbo      # whisper model name
RETRY_MAX=3              # API request retries
RETRY_BASE_DELAY=2       # exponential backoff base (seconds)
```

### 2. Check for pending jobs

```bash
./run.sh --status
```

Output:

```
Checking for pending jobs at https://listen.silent.energy...

  Pending jobs: 2
  Worker URL:   https://listen.silent.energy
  Mode:         docker
  Poll interval: 60s

Jobs:
  5e2f561a...  recording.wav                     3.2 MB
  0ec144db...  meeting-notes.mp3                12.1 MB
```

### 3. Start the worker

```bash
./run.sh
```

This checks prerequisites (Docker, NVIDIA runtime), loads config from `.env`, and streams logs to your terminal. Ctrl+C to stop.

When a job comes in:

```
2026-03-18 19:00:20 [INFO] === Processing job 5e2f561a...: recording.wav ===
2026-03-18 19:00:20 [INFO]   [claim] 0.142s
2026-03-18 19:00:21 [INFO]   [download] 0.891s
2026-03-18 19:00:22 [INFO]   [transcribe] 1.320s
2026-03-18 19:00:23 [INFO]   [upload] 0.254s
2026-03-18 19:00:23 [INFO] === Job 5e2f561a... completed ===
  claim: 0.142s
  download: 0.891s
  transcribe: 1.320s
  upload: 0.254s
  TOTAL: 2.607s
```

## How It Works

1. `run.sh` checks prerequisites (Docker, NVIDIA runtime), loads config from `../.env`
2. `worker.py` starts polling `GET /api/jobs?status=pending` every `POLL_INTERVAL` seconds
3. When a pending job is found, it atomically claims it (`PATCH` to `processing`)
4. Audio is downloaded to a temp directory (`/tmp/whisper-<id>/`) with `0700` permissions
5. **Docker mode**: spawns a Docker container using the existing `Dockerfile` to transcribe
6. **Direct mode**: runs faster-whisper in-process (requires `pip install faster-whisper`)
7. Results (transcript.txt + segments.json) are uploaded to the API
8. Audio is auto-deleted from cloud storage after successful upload
9. Temp files are cleaned up

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `API_URL` | *(required)* | Cloudflare Worker URL |
| `WORKER_TOKEN` | *(required)* | Bearer token for worker authentication |
| `WORKER_ID` | auto-generated | Unique ID for this worker (persisted in `~/.whisper-worker-id`) |
| `POLL_INTERVAL` | `5` | Seconds between polling for new jobs |
| `MAX_CONCURRENT_JOBS` | `1` | Maximum parallel jobs (requires sufficient VRAM) |
| `MODE` | `docker` | `docker` (containerized) or `direct` (in-process) |
| `WHISPER_MODEL` | `turbo` | Whisper model name |
| `RETRY_MAX` | `3` | Max API request retries |
| `RETRY_BASE_DELAY` | `2` | Base delay for exponential backoff (seconds) |

## Worker ID

If `WORKER_ID` is not set in `.env`, a random ID is generated and saved to `~/.whisper-worker-id`. This ID persists across restarts and appears in the `worker_id` field of each job in the database — so you can tell which machine processed which job.

## Multiple Workers

To run multiple GPU workers (e.g., on different machines):

1. Copy `gpu-worker/` to each machine
2. Give each a unique `WORKER_ID` (or let it auto-generate)
3. Use the same `API_URL` and `WORKER_TOKEN`

Workers atomically claim jobs — no duplicate processing.

## Docker Compose Mode (Alternative)

Not yet implemented. Use `run.sh` for now.

## Graceful Shutdown

Ctrl+C (or `SIGTERM`) will:
1. Stop polling for new jobs
2. Wait for the current job to finish
3. Clean up temp files
4. Exit cleanly

## Troubleshooting

### "Failed to claim job"

Another worker may have claimed it first, or the job was deleted. This is normal — the worker will pick up the next available job.

### "CUDA out of memory"

- Use a smaller model: `WHISPER_MODEL=small` or `WHISPER_MODEL=base`
- Reduce `MAX_CONCURRENT_JOBS` to 1
- Close other GPU-intensive applications

### "Dockerfile not found"

The worker looks for `../Dockerfile` relative to the `gpu-worker/` directory. Make sure the repo structure is intact.

### Jobs stuck in "processing"

Jobs automatically reset to "pending" after 30 minutes. If your jobs take longer than 30 minutes, the timeout can be adjusted in the Worker source (`auth.ts` — `STALE_JOB_MINUTES`).

### Worker not picking up jobs

1. Check `API_URL` is reachable: `./run.sh --status`
2. Check `WORKER_TOKEN` matches: compare with `grep WORKER_TOKEN ../.env`
3. Check logs for errors
