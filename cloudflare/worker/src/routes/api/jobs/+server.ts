import { json, requireAuth, resetStaleJobs, validateFile, r2AudioKey } from "$lib/server/auth";
import type { RequestHandler } from "./$types";
import type { Job, CreateJobResponse, PendingJobsResponse } from "$lib/types";

export const GET: RequestHandler = async ({ request, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

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
};

export const POST: RequestHandler = async ({ request, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

  const formData = await request.formData();
  const raw = formData.get("file");

  if (!raw || typeof raw === "string") {
    return json({ error: "No file provided. Use multipart/form-data with a 'file' field." }, 400);
  }

  const file = raw as unknown as File;
  const filename = file.name || "audio.wav";

  const validated = validateFile(filename, file.size);
  if (validated instanceof Response) return validated;

  const jobId = crypto.randomUUID();

  await env.DB.prepare(
    `INSERT INTO jobs (id, status, original_filename, file_size, file_type, created_at, updated_at)
     VALUES (?, 'pending', ?, ?, ?, datetime('now'), datetime('now'))`,
  ).bind(jobId, filename, file.size, file.type || null).run();

  await env.R2_BUCKET.put(r2AudioKey(jobId, validated.ext), file.stream(), {
    httpMetadata: { contentType: file.type || "application/octet-stream" },
  });

  const body: CreateJobResponse = {
    id: jobId,
    status: "pending",
    original_filename: filename,
    file_size: file.size,
  };

  return json(body, 201);
};
