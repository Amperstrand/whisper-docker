# GPU Worker Setup Guide

The GPU worker polls the Cloudflare Worker API for pending transcription jobs, downloads audio, runs faster-whisper on your GPU, and uploads results.

## Prerequisites

- NVIDIA GPU with driver installed
- Docker Engine + Compose plugin + NVIDIA Container Toolkit
- Python 3.10+ (for direct mode)

Run `sudo ./setup.sh` from the project root if you need to install Docker and NVIDIA toolkit.

## Quick Start

### Docker Mode (Recommended)

This mode uses the existing `Dockerfile` to run transcription in a container, managed by the worker agent.

1. **Configure**:
   ```bash
   cd gpu-worker
   cp config.example.env .env
   ```

   Edit `.env`:
   ```env
   API_URL=https://whisper-transcribe.your-subdomain.workers.dev
   WORKER_TOKEN=<same token from project root .env>
   WORKER_ID=gpu-1
   MODE=docker
   ```

2. **Build and run**:
   ```bash
   docker compose -f compose.worker.yaml up --build
   ```

### Direct Mode

This mode runs faster-whisper directly in the worker process (no Docker wrapper). Faster startup, but requires Python + faster-whisper on the host.

1. **Install dependencies**:
   ```bash
   pip install requests faster-whisper
   ```

2. **Configure**:
   ```env
   MODE=direct
   WHISPER_MODEL=turbo
   ```

3. **Run**:
   ```bash
   python3 worker.py
   ```

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `API_URL` | *(required)* | Cloudflare Worker URL (e.g., `https://whisper-transcribe.example.workers.dev`) |
| `WORKER_TOKEN` | *(required)* | Bearer token matching the Worker's `WORKER_TOKEN` secret |
| `WORKER_ID` | `gpu-1` | Unique identifier for this worker instance |
| `POLL_INTERVAL` | `5` | Seconds between polling for new jobs |
| `MAX_CONCURRENT_JOBS` | `1` | Maximum parallel jobs (requires sufficient VRAM) |
| `MODE` | `docker` | `docker` (containerized) or `direct` (in-process) |
| `WHISPER_MODEL` | `turbo` | Whisper model name (direct mode) |
| `RETRY_MAX` | `3` | Max API request retries |
| `RETRY_BASE_DELAY` | `2` | Base delay for exponential backoff (seconds) |

## Running with Docker Compose

The `compose.worker.yaml` mounts the parent repo directory (read-only) so the worker can invoke `docker compose` for transcription. It also mounts the Docker socket.

```bash
# Start in background
docker compose -f compose.worker.yaml up -d --build

# View logs
docker compose -f compose.worker.yaml logs -f

# Stop
docker compose -f compose.worker.yaml down
```

## Multiple Workers

To run multiple GPU workers (e.g., on different machines):

1. Deploy the same `gpu-worker/` to each machine
2. Give each a unique `WORKER_ID` (e.g., `gpu-1`, `gpu-2`)
3. Use the same `API_URL` and `WORKER_TOKEN`

Workers atomically claim jobs — no duplicate processing.

## Graceful Shutdown

The worker handles `SIGINT` (Ctrl+C) and `SIGTERM` (docker stop) gracefully. It will:
1. Stop polling for new jobs
2. Wait for the current job to finish
3. Exit cleanly

## Troubleshooting

### "Failed to claim job"

Another worker may have claimed it first, or the job was deleted. This is normal — the worker will pick up the next available job.

### "CUDA out of memory"

- Use a smaller model: `WHISPER_MODEL=small` or `WHISPER_MODEL=base`
- Reduce `MAX_CONCURRENT_JOBS` to 1
- Close other GPU-intensive applications

### Docker mode: "Dockerfile not found"

The worker looks for `../Dockerfile` relative to the `gpu-worker/` directory. Make sure the repo structure is intact.

### Jobs stuck in "processing"

Jobs automatically reset to "pending" after 30 minutes. If your jobs take longer than 30 minutes, the timeout can be adjusted in the Worker source (`routes.ts` — `STALE_JOB_MINUTES`).

### Worker not picking up jobs

1. Check `API_URL` is correct and reachable: `curl <API_URL>/health`
2. Check `WORKER_TOKEN` matches: compare with `grep WORKER_TOKEN ../.env`
3. Check logs for errors: `docker compose -f compose.worker.yaml logs -f`
