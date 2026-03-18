import indexHtml from "../frontend/index.html";
import stylesCss from "../frontend/styles.css";
import appJs from "../frontend/app.js";

const STATIC_ASSETS: Record<string, { content: string; type: string }> = {
  "/": { content: indexHtml, type: "text/html;charset=UTF-8" },
  "/index.html": { content: indexHtml, type: "text/html;charset=UTF-8" },
  "/styles.css": { content: stylesCss, type: "text/css;charset=UTF-8" },
  "/app.js": { content: appJs, type: "application/javascript;charset=UTF-8" },
};

export function serveStatic(pathname: string): Response | null {
  const asset = STATIC_ASSETS[pathname];
  if (!asset) {
    return null;
  }
  return new Response(asset.content, {
    headers: { "Content-Type": asset.type, "Cache-Control": "public, max-age=3600" },
  });
}
