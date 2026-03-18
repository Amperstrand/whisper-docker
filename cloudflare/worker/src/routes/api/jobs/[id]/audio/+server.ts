import { json, requireAuth, getExtension } from "$lib/server/auth";
import type { RequestHandler } from "./$types";

export const GET: RequestHandler = async ({ request, params, platform }) => {
  const env = platform?.env;
  if (!env) return json({ error: "Service unavailable" }, 503);

  const authErr = requireAuth(request, env);
  if (authErr) return authErr;

  const audioObjects = await env.R2_BUCKET.list({ prefix: `audio/${params.id}` });
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
  headers.set("Content-Disposition", `attachment; filename="${params.id}${getExtension(key)}"`);

  return new Response(object.body, { headers });
};
