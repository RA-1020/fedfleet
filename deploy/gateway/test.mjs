// node deploy/gateway/test.mjs — checks the gateway with a fake VPC binding and limiter.
import assert from "node:assert/strict";
import worker from "./src/index.js";

const calls = [];
let allow = true;
const env = {
  API_TOKEN: "user-token", ORIGIN_KEY: "origin-key",
  CONTROL_API: { fetch: async (u, init) => { calls.push({ u, init }); return new Response('{"ok":1}', { status: 200, headers: { "content-type": "application/json" } }); } },
  LIMITER: { limit: async () => ({ success: allow }) },
};
const req = (path, opts = {}) => worker.fetch(new Request("https://gw.example" + path, opts), env);
const auth = { authorization: "Bearer user-token" };

assert.equal((await req("/")).status, 200);
assert.equal((await req("/admin", { headers: auth })).status, 404);
assert.equal((await req("/runs", { method: "DELETE", headers: auth })).status, 404);
assert.equal((await req("/status")).status, 401);
assert.equal((await req("/status", { headers: { authorization: "Bearer nope" } })).status, 401);
assert.equal(calls.length, 0, "nothing forwarded without a valid token");

let r = await req("/status", { headers: auth });
assert.equal(r.status, 200);
assert.equal(calls[0].u, "http://127.0.0.1:8088/status");
assert.equal(calls[0].init.headers.get("x-fedfleet-origin-key"), "origin-key");
assert.equal(calls[0].init.headers.get("authorization"), null, "user token not passed upstream");

r = await req("/runs", { method: "POST", headers: { ...auth, "content-type": "application/json" }, body: '{"name":"demo","rounds":2}' });
assert.equal(r.status, 200);
assert.equal(calls[1].init.body, '{"name":"demo","rounds":2}');
assert.equal((await req("/runs", { method: "POST", headers: auth, body: "x".repeat(3000) })).status, 413);
assert.equal((await req("/runs/fedfleet-runs-e9/full-lr0.0005", { headers: auth })).status, 200);

assert.equal((await req("/runs/a/../../etc", { headers: auth })).status, 404);
allow = false;
assert.equal((await req("/status", { headers: auth })).status, 429);
allow = true;
env.CONTROL_API.fetch = async () => { throw new Error("tunnel down"); };
assert.equal((await req("/status", { headers: auth })).status, 502);
console.log("ok gateway: routes, token, origin key, body limit, rate limit, tunnel failure");
