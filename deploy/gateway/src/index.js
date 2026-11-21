// FedFleet gateway (WBS 12.1, minimal): the public way into the control API.
// 1. Only allow-listed routes pass. 2. The caller must send `Authorization: Bearer <API_TOKEN>`.
// 3. A per-caller rate limit applies. 4. The request goes to the control API on the Oracle VM
//    through a Workers VPC Service (a Cloudflare Tunnel from the VM; no public address, no
//    inbound port), with the origin key the API checks.
// Secrets (wrangler secret put): API_TOKEN, ORIGIN_KEY. Bindings (wrangler.toml): CONTROL_API, LIMITER.

const ROUTES = [
  ["GET", /^\/healthz$/],
  ["GET", /^\/status$/],
  ["GET", /^\/runs$/],
  ["GET", /^\/runs\/[a-z0-9][a-z0-9.\-/]{0,80}$/],
  ["POST", /^\/runs$/],
];
const ORIGIN = "http://127.0.0.1:8088";

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function sameToken(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a), y = enc.encode(b);
  if (x.length !== y.length) return false;
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" && request.method === "GET") {
      return json(200, { service: "FedFleet gateway", routes: ROUTES.map(([m, r]) => `${m} ${r.source}`) });
    }
    if (url.pathname.includes("..") || !ROUTES.some(([m, r]) => m === request.method && r.test(url.pathname))) {
      return json(404, { error: "no such route" });
    }
    if (!env.API_TOKEN || !env.ORIGIN_KEY) return json(503, { error: "gateway secrets not set" });
    const auth = request.headers.get("authorization") || "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!token || !sameToken(token, env.API_TOKEN)) return json(401, { error: "missing or wrong token" });

    if (env.LIMITER) {
      const key = request.headers.get("cf-connecting-ip") || "unknown";
      const { success } = await env.LIMITER.limit({ key: `${key}:${request.method}` });
      if (!success) return json(429, { error: "rate limit exceeded, try again in a minute" });
    }

    const headers = new Headers({ "x-fedfleet-origin-key": env.ORIGIN_KEY });
    let body;
    if (request.method === "POST") {
      headers.set("content-type", "application/json");
      body = await request.text();
      if (body.length > 2000) return json(413, { error: "body too large" });
    }
    try {
      const upstream = await env.CONTROL_API.fetch(ORIGIN + url.pathname, { method: request.method, headers, body });
      return new Response(upstream.body, {
        status: upstream.status,
        headers: { "content-type": upstream.headers.get("content-type") || "application/json", "cache-control": "no-store" },
      });
    } catch (e) {
      return json(502, { error: "control API unreachable through the tunnel" });
    }
  },
};
