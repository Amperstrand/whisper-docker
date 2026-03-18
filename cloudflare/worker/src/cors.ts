import type { Env } from "./types";

const ALLOWED_ORIGINS = ["*"];

export function getCorsOrigin(env: Env): string {
  const configured = env.CORS_ORIGIN;
  if (configured && configured !== "*") {
    return configured;
  }
  return "*";
}

export function corsHeaders(env: Env, request?: Request): Record<string, string> {
  const origin = getCorsOrigin(env);
  const requestOrigin = request?.headers.get("Origin");
  const allowOrigin = origin === "*" ? "*" : (requestOrigin ?? origin);

  return {
    "Access-Control-Allow-Origin": allowOrigin,
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    ...(origin !== "*" ? { "Access-Control-Allow-Credentials": "true" } : {}),
  };
}

export function handleCors(request: Request, env: Env): Response | null {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: corsHeaders(env, request),
    });
  }
  return null;
}
