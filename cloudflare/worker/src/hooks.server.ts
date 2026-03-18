import type { Handle } from "@sveltejs/kit";

export const handle: Handle = async ({ event, resolve }) => {
  if (event.url.pathname.startsWith("/api/")) {
    if (event.request.method === "OPTIONS") {
      const origin = event.platform?.env.CORS_ORIGIN || "*";
      const requestOrigin = event.request.headers.get("Origin");
      const allowOrigin = origin === "*" ? "*" : requestOrigin || origin;

      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": allowOrigin,
          "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
          ...(origin !== "*" ? { "Access-Control-Max-Age": "86400" } : {}),
        },
      });
    }

    const response = await resolve(event);

    const origin = event.platform?.env.CORS_ORIGIN || "*";
    const requestOrigin = event.request.headers.get("Origin");
    const allowOrigin = origin === "*" ? "*" : requestOrigin || origin;

    response.headers.set("Access-Control-Allow-Origin", allowOrigin);
    response.headers.set("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS");
    response.headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization");

    return response;
  }

  const response = await resolve(event);

  if (response.headers.get("content-type")?.includes("text/html")) {
    response.headers.set("Cache-Control", "no-cache");
  }

  return response;
};
