import { json, requireAuth, r2TranscriptKey, r2SegmentsKey, r2AnalysisKey } from "$lib/server/auth";
import type { RequestHandler } from "./$types";
import type { SuccessResponse } from "$lib/types";

export const POST: RequestHandler = async ({ request, params, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

  const authErr = requireAuth(request, env);
  if (authErr) return authErr;

  const row = await env.DB.prepare("SELECT status FROM jobs WHERE id = ?")
    .bind(params.id)
    .first<{ status: string }>();

  if (!row) return json({ error: "Job not found" }, 404);

  if (row.status !== "processing") {
    return json({ error: `Job is '${row.status}', expected 'processing'` }, 400);
  }

  const formData = await request.formData();
  const transcript = formData.get("transcript") as File | null;
  const segments = formData.get("segments") as File | null;
  const analysis = formData.get("analysis") as File | null;

  if (!transcript || !segments) {
    return json({ error: "Both 'transcript' and 'segments' fields required" }, 400);
  }

  const uploads: Promise<void>[] = [
    env.R2_BUCKET.put(r2TranscriptKey(params.id), transcript.stream(), {
      httpMetadata: { contentType: "text/plain; charset=utf-8" },
    }),
    env.R2_BUCKET.put(r2SegmentsKey(params.id), segments.stream(), {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
    }),
  ];

  if (analysis) {
    uploads.push(
      env.R2_BUCKET.put(r2AnalysisKey(params.id), analysis.stream(), {
        httpMetadata: { contentType: "application/json; charset=utf-8" },
      }),
    );
  }

  await Promise.all(uploads);

  const audioObjects = await env.R2_BUCKET.list({ prefix: `audio/${params.id}` });
  await Promise.allSettled(audioObjects.objects.map((obj) => env.R2_BUCKET.delete(obj.key)));

  await env.DB.prepare(
    `UPDATE jobs SET status = 'completed', completed_at = datetime('now'), updated_at = datetime('now') WHERE id = ?`,
  )
    .bind(params.id)
    .run();

  const body: SuccessResponse = { success: true };
  return json(body);
};
