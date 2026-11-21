# FedFleet gateway (WBS 12.1, minimal)

A Cloudflare Worker in front of the control API (`deploy/control_api`). It passes only these
routes: `GET /healthz`, `GET /status`, `GET /runs`, `GET /runs/<name>`, `POST /runs`. For each
request it:

1. checks `Authorization: Bearer <API_TOKEN>` (a Worker secret);
2. applies a rate limit of 20 requests a minute per caller IP and method;
3. forwards to the control API through a **Workers VPC Service**: a Cloudflare Tunnel run by
   `cloudflared` on the Oracle VM, which connects outward only. The API has no public address
   and the VM opens no inbound port. The Worker adds `X-FedFleet-Origin-Key` (secret
   `ORIGIN_KEY`, the same value as on the VM), and never passes the caller's token upstream.

Workers VPC was in beta (free on all plans) when this was built. Without a domain, a named
public tunnel hostname is not possible, which is why the VPC binding is used.

Tests: `node deploy/gateway/test.mjs` (fake binding and limiter).

Set-up, in order (details in the main plan doc):
1. `npx wrangler tunnel create fedfleet-agg` (prints the tunnel id).
2. Install `cloudflared` on the VM and run the tunnel as a service with its token.
3. `npx wrangler vpc service create fedfleet-control-api --type http --tunnel-id <id> --ipv4 127.0.0.1 --http-port 8088`,
   then put the printed service id into `wrangler.toml`.
4. `npx wrangler deploy`, then `wrangler secret put ORIGIN_KEY` (piped from the VM) and `API_TOKEN`.
