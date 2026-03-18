# API Documentation

Base URL: `https://whisper-transcribe.<subdomain>.workers.dev`

## Authentication

**Public endpoints** require no authentication. The job UUID serves as the access token.

**Worker endpoints** require a Bearer token:

```
Authorization: Bearer <WORKER_TOKEN>
```

## Response Format

All responses are JSON. Errors use this format:

```json
{
  "error": "Description of the error"
}
```

## Endpoints

### Health Check

```
GET /health
```

**Response** `200`:
```json
{
  "status": "ok",
  "timestamp": "2026-03-18T12:00:00.000Z"
}
```

---

### Create Job

```
POST /api/jobs
Content-Type: multipart/form-data
```

**Body**: `file` — audio file (WAV, MP3, M4A, FLAC, OGG, WebM, max 100 MB)

**Response** `201`:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "original_filename": "recording.mp3",
  "file_size": 5242880
}
```

**Errors**:
- `400` — No file, unsupported type, or file too large

---

### Get Job Status

```
GET /api/jobs/:id
```

**Response** `200`:
```json
{
  "job": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "processing",
    "original_filename": "recording.mp3",
    "file_size": 5242880,
    "file_type": "audio/mpeg",
    "created_at": "2026-03-18T12:00:00.000Z",
    "updated_at": "2026-03-18T12:00:05.000Z",
    "started_at": "2026-03-18T12:00:03.000Z",
    "completed_at": null,
    "worker_id": "gpu-1",
    "error_message": null
  }
}
```

**Errors**: `404` — Job not found

---

### Get Transcription Result

```
GET /api/jobs/:id/result
```

**When pending/processing** `200`:
```json
{
  "id": "550e8400-...",
  "status": "pending",
  "transcript": null,
  "segments": null
}
```

**When completed** `200`:
```json
{
  "id": "550e8400-...",
  "status": "completed",
  "transcript": "Hello, this is a transcription.\nThe second segment here.",
  "segments": [
    {
      "start": 0.0,
      "end": 3.52,
      "text": "Hello, this is a transcription.",
      "words": [
        {"word": " Hello,", "start": 0.0, "end": 0.36, "probability": 0.9053}
      ]
    }
  ]
}
```

**When failed** `200`:
```json
{
  "id": "550e8400-...",
  "status": "failed",
  "transcript": null,
  "segments": null,
  "error": "CUDA out of memory"
}
```

---

### Delete Job

```
DELETE /api/jobs/:id
```

Deletes the job record and all associated R2 objects (audio, transcript, segments).

**Response** `200`:
```json
{
  "success": true
}
```

**Errors**: `404` — Job not found

---

### List Jobs (Worker Auth)

```
GET /api/jobs?status=pending&limit=10
Authorization: Bearer <WORKER_TOKEN>
```

**Query parameters**:
- `status` — Filter by status (`pending`, `processing`, `completed`, `failed`)
- `limit` — Max results (1–50, default 1)

Note: When `status=pending`, any jobs stuck in `processing` for >30 minutes are automatically reset to `pending`.

**Response** `200`:
```json
{
  "jobs": [
    {
      "id": "550e8400-...",
      "status": "pending",
      "original_filename": "audio.wav",
      "file_size": 1048576,
      "created_at": "2026-03-18T12:00:00.000Z"
    }
  ]
}
```

---

### Update Job (Worker Auth)

```
PATCH /api/jobs/:id
Authorization: Bearer <WORKER_TOKEN>
Content-Type: application/json
```

**Body**:
```json
{
  "status": "processing",
  "worker_id": "gpu-1"
}
```

Or to report failure:
```json
{
  "status": "failed",
  "error_message": "CUDA out of memory"
}
```

**Response** `200`:
```json
{
  "success": true
}
```

---

### Get Audio File (Worker Auth)

```
GET /api/jobs/:id/audio
Authorization: Bearer <WORKER_TOKEN>
```

Returns the raw audio file as a binary stream.

**Response** `200`: Binary audio data with appropriate `Content-Type` and `Content-Disposition: attachment` headers.

**Errors**:
- `401` — Unauthorized
- `404` — Audio file not found

---

### Upload Results (Worker Auth)

```
POST /api/jobs/:id/results
Authorization: Bearer <WORKER_TOKEN>
Content-Type: multipart/form-data
```

**Body**:
- `transcript` — text file (plain text transcript)
- `segments` — JSON file (segment data with timestamps)

On success, the audio file is automatically deleted from R2.

**Response** `200`:
```json
{
  "success": true
}
```

**Errors**:
- `400` — Missing fields, or job not in `processing` status
- `401` — Unauthorized
- `404` — Job not found

---

## Job Status Flow

```
pending → processing → completed
                  ↘ failed
```

Jobs stuck in `processing` for >30 minutes are automatically reset to `pending` on the next pending job query.

## File Size Limits

- **Upload**: 100 MB max (enforced at Worker level)
- **Audio download**: No limit (streams directly from R2)
- **Results upload**: No hard limit (transcripts are small)

## CORS

All responses include CORS headers. The allowed origin is configurable via the `CORS_ORIGIN` environment variable (defaults to `*`).
