import {
  json,
  requireAuth,
  getJob,
  r2TranscriptKey,
  r2SegmentsKey,
  r2AudioKey,
  getExtension,
} from "$lib/server/auth";
import type { RequestHandler } from "./$types";
import type { JobResponse, SuccessResponse, TranscriptResult } from "$lib/types";

export const GET: RequestHandler = async ({ params, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

  const row = await getJob(env.DB, params.id);
  if (!row) return json({ error: "Job not found" }, 404);

  const body: JobResponse = { job: row };
  return json(body);
};

export const DELETE: RequestHandler = async ({ params, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

  const row = await getJob(env.DB, params.id);
  if (!row) return json({ error: "Job not found" }, 404);

  await Promise.allSettled([
    env.R2_BUCKET.delete(r2TranscriptKey(params.id)),
    env.R2_BUCKET.delete(r2SegmentsKey(params.id)),
  ]);

  const audioObjects = await env.R2_BUCKET.list({ prefix: `audio/${params.id}` });
  await Promise.allSettled(audioObjects.objects.map((obj) => env.R2_BUCKET.delete(obj.key)));

  await env.DB.prepare("DELETE FROM jobs WHERE id = ?").bind(params.id).run();

  const body: SuccessResponse = { success: true };
  return json(body);
};

export const PATCH: RequestHandler = async ({ request, params, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

  const authErr = requireAuth(request, env);
  if (authErr) return authErr;

  const row = await getJob(env.DB, params.id);
  if (!row) return json({ error: "Job not found" }, 404);

  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
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

  values.push(params.id);

  await env.DB.prepare(`UPDATE jobs SET ${sets.join(", ")} WHERE id = ?`).bind(...values).run();

  const updated = await getJob(env.DB, params.id);
  return json({ success: true, job: updated });
};
