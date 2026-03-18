# Deployment Guide

## Prerequisites

1. **Cloudflare account** — free tier works
2. **Node.js 18+** — for wrangler CLI
3. **NVIDIA GPU + Docker** — for the GPU worker (see [WORKER.md](WORKER.md))
4. **Git** — to clone the repository

## Step 1: Clone and Configure

```bash
git clone https://github.com/Amperstrand/whisper-docker.git
cd whisper-docker
```

Create the deployment environment file:

```bash
cp -n .env .env  # .env is gitignored — safe to store secrets here
```

The first time you run `./deploy.sh`, it will create a `.env` template for you. Fill in these values:

| Variable | Description | How to get it |
|---|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Your Cloudflare account ID | Dashboard URL or `npx wrangler whoami` |
| `R2_BUCKET_NAME` | R2 bucket name | Any name, default: `whisper-transcriptions` |
| `D1_DATABASE_NAME` | D1 database name | Any name, default: `whisper-transcriptions` |
| `WORKER_TOKEN` | Bearer token for GPU workers | Auto-generated if blank, or run `openssl rand -hex 32` |
| `CORS_ORIGIN` | Allowed frontend origin | `*` for open, or your domain |

## Step 2: Authenticate with Cloudflare

```bash
npx wrangler login
```

This opens a browser window to authorize wrangler. Verify with:

```bash
npx wrangler whoami
```

## Step 3: Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

The script handles everything:

1. Validates prerequisites (Node.js, wrangler auth)
2. Creates R2 bucket (if not exists)
3. Creates D1 database (if not exists)
4. Applies database schema
5. Configures `wrangler.toml` with your account/database IDs
6. Installs npm dependencies
7. Deploys the Worker
8. Sets the `WORKER_TOKEN` secret

You'll see output like:

```
==========================================
  Deployment complete!
==========================================

  Worker URL:  https://whisper-transcribe.your-subdomain.workers.dev
  Health:      https://whisper-transcribe.your-subdomain.workers.dev/health
```

## Step 4: Verify

```bash
curl https://whisper-transcribe.your-subdomain.workers.dev/health
# {"status":"ok","timestamp":"..."}
```

Open the URL in your browser — you should see the upload interface.

## Step 5: Start the GPU Worker

```bash
cd gpu-worker
cp config.example.env .env
```

Edit `gpu-worker/.env`:

```env
API_URL=https://whisper-transcribe.your-subdomain.workers.dev
WORKER_TOKEN=<same token from project .env>
WORKER_ID=gpu-1
```

Start the worker:

```bash
# Docker mode (recommended — uses the existing Dockerfile)
docker compose -f compose.worker.yaml up --build

# Or direct mode (no Docker wrapper, faster):
python3 worker.py  # requires faster-whisper installed
```

## Step 6: Test End-to-End

1. Open the Worker URL in your browser
2. Upload an audio file (WAV, MP3, M4A, FLAC, OGG, or WebM)
3. Watch the status update from "Pending" to "Processing" to "Completed"
4. View and download the transcript

## Custom Domain (Optional)

### Worker custom domain

In `cloudflare/worker/wrangler.toml`, add:

```toml
routes = [
  { pattern = "transcribe.yourdomain.com", custom_domain = true }
]
```

Then re-run `./deploy.sh`.

### CORS

If using a custom domain, update `.env`:

```env
CORS_ORIGIN=https://transcribe.yourdomain.com
```

## Troubleshooting

### "wrangler is not authenticated"

Run `npx wrangler login` again. Tokens expire.

### "database_id = PLACEHOLDER" in wrangler.toml

Delete the line and re-run `deploy.sh`, or manually set the ID from `npx wrangler d1 list`.

### Worker returns 500 errors

Check logs:

```bash
cd cloudflare/worker
npx wrangler tail
```

### GPU worker can't connect

- Verify `API_URL` includes `https://` and no trailing slash
- Verify `WORKER_TOKEN` matches between `.env` and `gpu-worker/.env`
- Check the Worker is deployed: `curl <API_URL>/health`

### Jobs stuck in "processing"

Jobs automatically reset to "pending" after 30 minutes. If a worker crashed, the job will be picked up again.

### Cleaning up old jobs

```bash
./scripts/cleanup.sh 30  # delete jobs older than 30 days
```
