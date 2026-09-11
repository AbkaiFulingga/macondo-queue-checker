/**
 * Macondo Queue Checker — CORS proxy (Cloudflare Worker).
 *
 * The Macondo API sends no CORS headers, so the static site cannot call it
 * from the browser. This stateless worker forwards a strict GET-only allowlist
 * of public read endpoints and adds permissive CORS + 60s cache.
 *
 * No auth headers are ever forwarded; nothing private can pass through it.
 * Deploy: see DEPLOY.md (one-time, then forget).
 */

const ALLOW = [
  // GET /projects/{id}
  /^\/api\/projects\/(\d{1,8})$/,
  // GET /projects/{id}/ships
  /^\/api\/projects\/(\d{1,8})\/ships$/,
  // GET /users/{username-or-uuid}
  /^\/api\/users\/([A-Za-z0-9_.:-]{1,64})$/,
];

const UPSTREAM = "https://macondo.hackclub.com";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Accept, Content-Type",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET") {
      return json({ error: "GET only" }, 405);
    }
    const path = url.pathname;
    if (!ALLOW.some((re) => re.test(path))) {
      return json({ error: "endpoint not allowed" }, 403);
    }

    const upstream = UPSTREAM + path;
    try {
      const resp = await fetch(upstream, {
        method: "GET",
        headers: {
          "Accept": "application/json",
          "User-Agent": "macondo-queue-checker-proxy/0.1",
        },
        cf: { cacheTtl: 60, cacheEverything: true },
      });
      const body = await resp.arrayBuffer();
      const headers = new Headers({
        "Content-Type": resp.headers.get("Content-Type") || "application/json",
        "Cache-Control": "public, max-age=60",
        ...CORS,
      });
      return new Response(body, { status: resp.status, headers });
    } catch (e) {
      return json({ error: "upstream fetch failed" }, 502);
    }
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}
