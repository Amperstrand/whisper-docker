import type { Job } from "$lib/types";

const MAX_FILE_SIZE = 100 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set([".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"]);
const STALE_JOB_MINUTES = 30;

export function isAuthorized(request: Request, env: App.Platform["env"]): boolean {
  const auth = request.headers.get("Authorization");
  if (!auth || !auth.startsWith("Bearer ")) return false;
  return auth.slice(7) === env.WORKER_TOKEN;
}

export function requireAuth(request: Request, env: App.Platform["env"]): Response | null {
  if (!isAuthorized(request, env)) {
    return json({ error: "Unauthorized" }, 401);
  }
  return null;
}

export function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function getExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot === -1 ? "" : filename.slice(dot).toLowerCase();
}

export function validateFile(
  filename: string,
  fileSize: number,
): { ext: string } | Response {
  const ext = getExtension(filename);
  if (!ext || !ALLOWED_EXTENSIONS.has(ext)) {
    return json(
      { error: `Unsupported file type '${ext}'. Allowed: ${[...ALLOWED_EXTENSIONS].join(", ")}` },
      400,
    );
  }
  if (fileSize > MAX_FILE_SIZE) {
    return json(
      { error: `File too large (${(fileSize / 1024 / 1024).toFixed(1)} MB). Maximum is 100 MB.` },
      400,
    );
  }
  return { ext };
}

export function r2AudioKey(jobId: string, ext: string): string {
  return `audio/${jobId}${ext}`;
}

export function r2TranscriptKey(jobId: string): string {
  return `results/${jobId}/transcript.txt`;
}

export function r2SegmentsKey(jobId: string): string {
  return `results/${jobId}/segments.json`;
}

export async function resetStaleJobs(db: D1Database): Promise<void> {
  await db
    .prepare(
      `UPDATE jobs SET status = 'pending', worker_id = NULL, started_at = NULL, updated_at = datetime('now')
       WHERE status = 'processing' AND started_at < datetime('now', ?)`,
    )
    .bind(`-${STALE_JOB_MINUTES} minutes`)
    .run();
}

export async function getJob(db: D1Database, id: string): Promise<Job | null> {
  return db.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first<Job>();
}
