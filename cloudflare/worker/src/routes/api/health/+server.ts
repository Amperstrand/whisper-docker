import { json } from "$lib/server/auth";
import type { RequestHandler } from "./$types";

export const GET: RequestHandler = async () => {
  return json({ status: "ok", timestamp: new Date().toISOString() });
};
