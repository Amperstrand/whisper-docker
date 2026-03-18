import type { Env, Job, CreateJobResponse, JobResponse, TranscriptResult, SuccessResponse, PendingJobsResponse } from "./types";

const MAX_FILE_SIZE = 100 * 1024 * 1024; // 100 MB
const ALLOWED_EXTENSIONS = new Set([".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"]);
const STALE_JOB_MINUTES = 30;

function isAuthorized(request: Request, env: Env): boolean {
  const auth = request.headers.get("Authorization");
  if (!auth || !auth.startsWith("Bearer ")) {
    return false;
  }
  return auth.slice(7) === env.WORKER_TOKEN;
}

function requireAuth(request: Request, env: Env): Response | null {
  if (!isAuthorized(request, env)) {
    return json({ error: "Unauthorized" }, 401);
  }
  return null;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function getExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot === -1 ? "" : filename.slice(dot).toLowerCase();
}

function r2AudioKey(jobId: string, ext: string): string {
  return `audio/${jobId}${ext}`;
}

function r2TranscriptKey(jobId: string): string {
  return `results/${jobId}/transcript.txt`;
}

function r2SegmentsKey(jobId: string): string {
  return `results/${jobId}/segments.json`;
}

async function resetStaleJobs(db: D1Database): Promise<void> {
  await db.prepare(
    `UPDATE jobs SET status = 'pending', worker_id = NULL, started_at = NULL, updated_at = datetime('now')
     WHERE status = 'processing' AND started_at < datetime('now', ?)`
  ).bind(`-${STALE_JOB_MINUTES} minutes`).run();
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export async function handleHealth(_request: Request, _env: Env): Promise<Response> {
  return json({ status: "ok", timestamp: new Date().toISOString() });
}

// ---------------------------------------------------------------------------
// Create job (POST /api/jobs)
// ---------------------------------------------------------------------------

export async function handleCreateJob(request: Request, env: Env): Promise<Response> {
  const formData = await request.formData();
  const raw = formData.get("file");

  if (!raw || typeof raw === "string") {
    return json({ error: "No file provided. Use multipart/form-data with a 'file' field." }, 400);
  }

  const file = raw as unknown as File;
  const filename = file.name || "audio.wav";
  const ext = getExtension(filename);

  if (!ext || !ALLOWED_EXTENSIONS.has(ext)) {
    return json({
      error: `Unsupported file type '${ext}'. Allowed: ${[...ALLOWED_EXTENSIONS].join(", ")}`,
    }, 400);
  }

  if (file.size > MAX_FILE_SIZE) {
    return json({ error: `File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Maximum is 100 MB.` }, 400);
  }

  const jobId = crypto.randomUUID();

  await env.DB.prepare(
    `INSERT INTO jobs (id, status, original_filename, file_size, file_type, created_at, updated_at)
     VALUES (?, 'pending', ?, ?, ?, datetime('now'), datetime('now'))`
  ).bind(jobId, filename, file.size, file.type || null).run();

  await env.R2_BUCKET.put(r2AudioKey(jobId, ext), file.stream(), {
    httpMetadata: { contentType: file.type || "application/octet-stream" },
  });

  const body: CreateJobResponse = {
    id: jobId,
    status: "pending",
    original_filename: filename,
    file_size: file.size,
  };

  return json(body, 201);
}

// ---------------------------------------------------------------------------
// Get job (GET /api/jobs/:id)
// ---------------------------------------------------------------------------

export async function handleGetJob(request: Request, env: Env, id: string): Promise<Response> {
  const row = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first<Job>();
  if (!row) {
    return json({ error: "Job not found" }, 404);
  }
  return json({ job: row });
}

// ---------------------------------------------------------------------------
// Get result (GET /api/jobs/:id/result)
// ---------------------------------------------------------------------------

export async function handleGetResult(_request: Request, env: Env, id: string): Promise<Response> {
  const row = await env.DB.prepare("SELECT status, error_message FROM jobs WHERE id = ?").bind(id).first<{
    status: string;
    error_message: string | null;
  }>();

  if (!row) {
    return json({ error: "Job not found" }, 404);
  }

  if (row.status === "pending" || row.status === "processing") {
    return json({ id, status: row.status, transcript: null, segments: null });
  }

  if (row.status === "failed") {
    return json({
      id,
      status: row.status,
      transcript: null,
      segments: null,
      error: row.error_message,
    });
  }

  const [transcriptObj, segmentsObj] = await Promise.all([
    env.R2_BUCKET.get(r2TranscriptKey(id)),
    env.R2_BUCKET.get(r2SegmentsKey(id)),
  ]);

  if (!transcriptObj || !segmentsObj) {
    return json({ error: "Result files not found in storage" }, 404);
  }

  const transcript = await transcriptObj.text();
  const segments = await segmentsObj.json();

  const body: TranscriptResult = {
    id,
    status: "completed",
    transcript,
    segments: segments as unknown[],
  };

  return json(body);
}

// ---------------------------------------------------------------------------
// Delete job (DELETE /api/jobs/:id)
// ---------------------------------------------------------------------------

export async function handleDeleteJob(_request: Request, env: Env, id: string): Promise<Response> {
  const row = await env.DB.prepare("SELECT id FROM jobs WHERE id = ?").bind(id).first<string>();
  if (!row) {
    return json({ error: "Job not found" }, 404);
  }

  await Promise.allSettled([
    env.R2_BUCKET.delete(r2TranscriptKey(id)),
    env.R2_BUCKET.delete(r2SegmentsKey(id)),
  ]);

  const audioObjects = await env.R2_BUCKET.list({ prefix: `audio/${id}` });
  await Promise.allSettled(audioObjects.objects.map((obj) => env.R2_BUCKET.delete(obj.key)));

  await env.DB.prepare("DELETE FROM jobs WHERE id = ?").bind(id).run();

  const body: SuccessResponse = { success: true };
  return json(body);
}

// ---------------------------------------------------------------------------
// Get pending jobs (GET /api/jobs?status=pending&limit=N) [auth required]
// ---------------------------------------------------------------------------

export async function handleListJobs(request: Request, env: Env): Promise<Response> {
  const authErr = requireAuth(request, env);
  if (authErr) return authErr;

  const url = new URL(request.url);
  const status = url.searchParams.get("status");
  const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "1", 10), 1), 50);

  if (status === "pending") {
    await resetStaleJobs(env.DB);
  }

  let query: string;
  const params: unknown[] = [];

  if (status) {
    query = `SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT ?`;
    params.push(status, limit);
  } else {
    query = `SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?`;
    params.push(limit);
  }

  const result = await env.DB.prepare(query).bind(...params).all<Job>();

  const body: PendingJobsResponse = { jobs: result.results };
  return json(body);
}

// ---------------------------------------------------------------------------
// Update job (PATCH /api/jobs/:id) [auth required]
// ---------------------------------------------------------------------------

export async function handlePatchJob(request: Request, env: Env, id: string): Promise<Response> {
  const authErr = requireAuth(request, env);
  if (authErr) return authErr;

  const row = await env.DB.prepare("SELECT id FROM jobs WHERE id = ?").bind(id).first<string>();
  if (!row) {
    return json({ error: "Job not found" }, 404);
  }

  let body: Record<string, unknown>;
  try {
    body = await request.json() as Record<string, unknown>;
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }

  const allowed = ["status", "worker_id", "error_message"];
  const sets: string[] = ["updated_at = datetime('now')"];
  const values: unknown[] = [];

  for (const field of allowed) {
    if (body[field] !== undefined) {
      sets.push(`${field} = ?`);
      values.push(body[field]);
    }
  }

  if (sets.length === 1) {
    return json({ error: "No valid fields to update" }, 400);
  }

  if (body.status === "processing") {
    sets.push("started_at = datetime('now')");
  } else if (body.status === "completed" || body.status === "failed") {
    sets.push("completed_at = datetime('now')");
  }

  values.push(id);

  await env.DB.prepare(`UPDATE jobs SET ${sets.join(", ")} WHERE id = ?`).bind(...values).run();

  const body2: SuccessResponse = { success: true };
  return json(body2);
}

// ---------------------------------------------------------------------------
// Get audio (GET /api/jobs/:id/audio) [auth required]
// ---------------------------------------------------------------------------

export async function handleGetAudio(_request: Request, env: Env, id: string): Promise<Response> {
  const authErr = requireAuth(_request, env);
  if (authErr) return authErr;

  const audioObjects = await env.R2_BUCKET.list({ prefix: `audio/${id}` });
  if (audioObjects.objects.length === 0) {
    return json({ error: "Audio file not found" }, 404);
  }

  const key = audioObjects.objects[0].key;
  const object = await env.R2_BUCKET.get(key);
  if (!object) {
    return json({ error: "Audio file not found in storage" }, 404);
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("Content-Length", object.size.toString());
  headers.set("Content-Disposition", `attachment; filename="${id}${getExtension(key)}"`);

  return new Response(object.body, { headers });
}

// ---------------------------------------------------------------------------
// Upload results (POST /api/jobs/:id/results) [auth required]
// ---------------------------------------------------------------------------

export async function handleUploadResults(request: Request, env: Env, id: string): Promise<Response> {
  const authErr = requireAuth(request, env);
  if (authErr) return authErr;

  const row = await env.DB.prepare("SELECT status FROM jobs WHERE id = ?").bind(id).first<{ status: string }>();
  if (!row) {
    return json({ error: "Job not found" }, 404);
  }

  if (row.status !== "processing") {
    return json({ error: `Job is '${row.status}', expected 'processing'` }, 400);
  }

  const formData = await request.formData();
  const transcript = formData.get("transcript") as File | null;
  const segments = formData.get("segments") as File | null;

  if (!transcript || !segments) {
    return json({ error: "Both 'transcript' and 'segments' fields required" }, 400);
  }

  await Promise.all([
    env.R2_BUCKET.put(r2TranscriptKey(id), transcript.stream(), {
      httpMetadata: { contentType: "text/plain; charset=utf-8" },
    }),
    env.R2_BUCKET.put(r2SegmentsKey(id), segments.stream(), {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
    }),
  ]);

  const audioObjects = await env.R2_BUCKET.list({ prefix: `audio/${id}` });
  await Promise.allSettled(audioObjects.objects.map((obj) => env.R2_BUCKET.delete(obj.key)));

  await env.DB.prepare(
    `UPDATE jobs SET status = 'completed', completed_at = datetime('now'), updated_at = datetime('now') WHERE id = ?`
  ).bind(id).run();

  const body: SuccessResponse = { success: true };
  return json(body);
}
