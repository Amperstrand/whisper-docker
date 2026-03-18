# Deployment Guide

## Prerequisites

1. **Cloudflare account** — free tier works
2. **Node.js 18+** — for SvelteKit build + wrangler CLI
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
cd cloudflare/worker
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

1. Validates prerequisites (Node.js, npx, wrangler auth)
2. Creates R2 bucket (if not exists)
3. Creates D1 database (if not exists)
4. Applies database schema
5. Installs npm dependencies
6. Builds SvelteKit (`vite build`)
7. Deploys the Worker (`wrangler deploy`)
8. Sets the `WORKER_TOKEN` secret

You'll see output like:

```
=========================================
  Deployment complete (production)!
=========================================

  Worker URL:  https://listen.silent.energy
  Health:      https://listen.silent.energy/health
```
==========================================
  Deployment complete!
==========================================

  Worker URL:  https://whisper-transcribe.your-subdomain.workers.dev
  Health:      https://whisper-transcribe.your-subdomain.workers.dev/health
```

## Step 4: Verify

```bash
curl https://listen.silent.energy/health
# {"status":"ok","timestamp":"..."}
```

Open the URL in your browser — you should see the transcription-first audio player.

## Step 5: Start the GPU Worker

The GPU worker reads config from the project root `.env` file (no separate `gpu-worker/.env` needed). Make sure these variables are set:

```env
API_URL=https://listen.silent.energy
WORKER_TOKEN=<same token from project .env>
```

Start the worker:

```bash
cd gpu-worker
./run.sh
```

## Step 6: Test End-to-End

1. Open the Worker URL in your browser
2. Drop an audio file onto the page — it plays locally
3. Click **Transcribe** to upload and queue the job
4. Watch the status update from "Queued" to "Transcribing..." to "Completed"
5. View the karaoke player with word-level highlighting

## Custom Domain (Optional)

The project is pre-configured with two environments in `wrangler.toml`:

- **Beta** (top-level): `beta.listen.silent.energy`
- **Production** (`[env.production]`): `listen.silent.energy`

To use your own domain, edit `cloudflare/worker/wrangler.toml`:

```toml
routes = [
  { pattern = "transcribe.yourdomain.com", custom_domain = true }
]
```

For production, update the `[env.production]` routes section.

Then re-run `./deploy.sh` or `./deploy.sh production`.

### CORS

If using a custom domain, update `.env`:

```env
CORS_ORIGIN=https://transcribe.yourdomain.com
```

## Troubleshooting

### "wrangler is not authenticated"

Run `cd cloudflare/worker && npx wrangler login` again. Tokens expire.

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

## Manual Build & Deploy

If you want to bypass `deploy.sh` and build/deploy manually:

```bash
cd cloudflare/worker
npm install
npm run build          # builds SvelteKit via vite
npx wrangler deploy    # deploys to beta
npx wrangler deploy --env production  # deploys to production
```
