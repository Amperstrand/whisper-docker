import type { Env } from "./types";
import { handleCors, corsHeaders } from "./cors";
import { serveStatic } from "./static";
import {
  handleHealth,
  handleCreateJob,
  handleGetJob,
  handleGetResult,
  handleDeleteJob,
  handleListJobs,
  handlePatchJob,
  handleGetAudio,
  handleUploadResults,
} from "./routes";

function addCorsHeaders(response: Response, env: Env, request: Request): Response {
  const headers = new Headers(response.headers);
  const cors = corsHeaders(env, request);
  for (const [key, value] of Object.entries(cors)) {
    headers.set(key, value);
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function matchRoute(pathname: string): { id?: string } | null {
  const jobMatch = pathname.match(/^\/api\/jobs\/([a-f0-9-]{36})(?:\/(result|audio|results))?$/);
  if (jobMatch) {
    return { id: jobMatch[1] };
  }
  if (pathname === "/api/jobs" || pathname.startsWith("/api/jobs?")) {
    return {};
  }
  return null;
}

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const { pathname } = url;

    const corsResponse = handleCors(request, env);
    if (corsResponse) return corsResponse;

    try {
      let response: Response;

      // Health
      if (pathname === "/health") {
        response = await handleHealth(request, env);
        return addCorsHeaders(response, env, request);
      }

      // API routes
      if (pathname.startsWith("/api/jobs")) {
        const jobMatch = pathname.match(
          /^\/api\/jobs\/([a-f0-9-]{36})(?:\/(result|audio|results))?$/
        );

        if (jobMatch) {
          const id = jobMatch[1];
          const sub = jobMatch[2];

          switch (request.method) {
            case "GET":
              if (sub === "result") {
                response = await handleGetResult(request, env, id);
              } else if (sub === "audio") {
                response = await handleGetAudio(request, env, id);
              } else {
                response = await handleGetJob(request, env, id);
              }
              break;
            case "DELETE":
              response = await handleDeleteJob(request, env, id);
              break;
            case "PATCH":
              response = await handlePatchJob(request, env, id);
              break;
            case "POST":
              if (sub === "results") {
                response = await handleUploadResults(request, env, id);
              } else {
                response = json({ error: "Not found" }, 404);
              }
              break;
            default:
              response = json({ error: "Method not allowed" }, 405);
          }
        } else if (pathname === "/api/jobs") {
          switch (request.method) {
            case "GET":
              response = await handleListJobs(request, env);
              break;
            case "POST":
              response = await handleCreateJob(request, env);
              break;
            default:
              response = json({ error: "Method not allowed" }, 405);
          }
        } else {
          response = json({ error: "Not found" }, 404);
        }

        return addCorsHeaders(response, env, request);
      }

      // Static assets
      const staticResponse = serveStatic(pathname);
      if (staticResponse) {
        return addCorsHeaders(staticResponse, env, request);
      }

      // 404
      return addCorsHeaders(json({ error: "Not found" }, 404), env, request);
    } catch (err) {
      console.error("Unhandled error:", err);
      const errorBody = err instanceof Error ? err.message : "Internal server error";
      return addCorsHeaders(json({ error: errorBody }, 500), env, request);
    }
  },
} satisfies ExportedHandler<Env>;

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
