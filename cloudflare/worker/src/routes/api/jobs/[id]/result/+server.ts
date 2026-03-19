import { json, getJob, r2TranscriptKey, r2SegmentsKey, r2AnalysisKey } from "$lib/server/auth";
import type { RequestHandler } from "./$types";
import type { TranscriptResult } from "$lib/types";

export const GET: RequestHandler = async ({ params, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

  const row = await env.DB.prepare("SELECT status, error_message FROM jobs WHERE id = ?")
    .bind(params.id)
    .first<{ status: string; error_message: string | null }>();

  if (!row) return json({ error: "Job not found" }, 404);

  if (row.status === "pending" || row.status === "processing") {
    return json({ id: params.id, status: row.status, transcript: null, segments: null });
  }

  if (row.status === "failed") {
    return json({
      id: params.id,
      status: row.status,
      transcript: null,
      segments: null,
      error: row.error_message,
    });
  }

  const [transcriptObj, segmentsObj, analysisObj] = await Promise.all([
    env.R2_BUCKET.get(r2TranscriptKey(params.id)),
    env.R2_BUCKET.get(r2SegmentsKey(params.id)),
    env.R2_BUCKET.get(r2AnalysisKey(params.id)),
  ]);

  if (!transcriptObj || !segmentsObj) {
    return json({ error: "Result files not found in storage" }, 404);
  }

  const transcript = await transcriptObj.text();
  const segments = await segmentsObj.json();
  const analysis = analysisObj ? await analysisObj.json() : null;

  const body: TranscriptResult = {
    id: params.id,
    status: "completed",
    transcript,
    segments: segments as unknown[],
    analysis: analysis as unknown,
  };

  return json(body);
};
