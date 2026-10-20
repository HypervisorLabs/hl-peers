# `hl-peers` — TypeScript client

**1.1.0**, for the Hyperliquid Peer API at `https://api.hypervisor.gg` (plain
paths at the host root: `/peers`, `/peers/{ip}`, `/peers/facets`,
`/network-map/graph`, `/network-map/summary`).

Query the Hyperliquid peer directory — *"peers in DE that serve the mempool"*,
*"every tier-1 peer with a free gossip slot"*, *"all sentries"* — and turn the
answer into an `override_gossip_config.json` your node can boot from.

- **Zero runtime dependencies.** Global `fetch` only.
- **ESM + CJS + `.d.ts`**, Node >= 18, `strict: true`.
- **Runtime-agnostic core**: the file-writing helpers live in a separate
  `…/gossip-fs` entry point, so importing the core never pulls in `node:fs`.
- **Byte-identical output** with the Elixir, Rust and Python clients.

---

## Install

From a local build of this repository (the published surface is `dist/`, and a
fresh clone has none — build it once first):

```bash
npm --prefix /path/to/hl-peers/typescript ci
npm --prefix /path/to/hl-peers/typescript run build
npm install file:/path/to/hl-peers/typescript
```

An npm release will follow.

## Quickstart

```ts
import { PeerApiClient, peerQuery } from 'hl-peers';

const client = new PeerApiClient({
  // baseUrl defaults to https://api.hypervisor.gg (or HL_PEER_API_BASE_URL)
  apiKey: process.env.HL_PEER_API_KEY, // omit for anonymous, a real tier
});

const result = await client.listPeers(peerQuery().cc(['DE']));
if ('notModified' in result) throw new Error('conditional request');

// 🔴 `available: false` is NOT "no peers matched" — it means the producer could
// not build a graph. Check it before `stale`.
if (!result.envelope.available) throw new Error('peer graph unavailable');

for (const p of result.peers) {
  console.log(p.id, p.tier ?? 'unplaced', p.cc, p.connectable, p.mempool.serves);
}
console.log(result.rateLimit.remaining, '/', result.rateLimit.limit, 'API', result.apiVersion);
```

The samples on this page are fragments of the programs in
[`examples/`](examples), which `test/examples.test.ts` executes against the
fixtures. They are not verbatim quotations, so `test/docs.test.ts` checks the
part that would actually break you: every import, method and option named here
is resolved against the real module, and a documented call that does not exist
fails the build.

## Common queries

```ts
import { mempoolServers, notFull, peerQuery, sentries, tier1 } from 'hl-peers';

await client.listPeers(mempoolServers().cc(['DE']));   // 1. serves mempool AND in DE
await client.listPeers(notFull({ cc: ['SG'] }));       // 2. has a free inbound slot
await client.listPeers(tier1());                       // 3. tier 1 — EXACT
await client.listPeers(sentries());                    // 4. label=sentry, not tier=0

// presets refine like any other query
const seeds = tier1().accepting(true).maxBlocksBehind(5).sort('random').limit(50);

// pagination is lazy and guarded
const walk = client.iterPeers(seeds, { maxItems: 500 });
for await (const peer of walk) console.log(peer.id);
if (walk.truncated) console.warn('stopped at the maxItems guard');

// discovery, so you never guess a label or a region — facets take every filter
const facets = await client.facets(peerQuery().cc(['DE']).servesMempool([true]));
```

## Every filter

| builder method | wire | notes |
|---|---|---|
| `chain('Mainnet')` | `chain` | closed enum |
| `cc(['DE'])` / `country(['DE'])` | `cc` / `country` | upper-cased; `country` is a server-side alias of `cc` (both ⇒ union) |
| `region(['aws:eu-central-1'])` | `region` | 🪤 cloud-hosted peers only |
| `asn(['AS64500'])` | `asn` | |
| `tier([1, null])` / `maxTier(2)` | `tier` / `max_tier` | exact vs ceiling; `null` = never placed |
| `role([...])`, `label(['sentry'])` | `role`, `label` | open sets |
| `connectable(['yes'])`, `accepting(true)` | `connectable`, `accepting` | three-state, never folded |
| `state(['responsive', 'open'])` | `state` | closed: `responsive` `open` `full` `syncing` `listening` `unreachable` `unknown` |
| `servesMempool([true, null])` | `serves_mempool` | closed: `true` `false` `null` — 🔴 `null` = unknown, **not** `false` |
| `auction(['holds_slot', 'bidding'])` | `auction` | closed: `holds_slot` `bidding` `bid_before` `null` |
| `maxBlocksBehind(5)`, `minUptime(0.95)` | … | |
| `maxLatencyMs(80).vantage('eu')` | … | `vantage` required with a latency bound |
| `seenWithin('1h')`, `version([...])` | … | `^[0-9]+[mhd]$` |
| `sort('tier')`, `limit(50)`, `cursor(…)`, `fields([...])` | … | not sent to `facets()` |
| `tryNewPeers(false)` | `try_new_peers` | only with the gossip-config helper |

Each peer carries typed `mempool` (`serves`, `last_checked`, `verdict`,
`first_frame_ms`, `source`) and `auction` (`status`, `slot`, `since`,
`last_bid_at`) objects — all-`null` means *unknown*, and a peer from an older
server that omits them decodes to the all-`null` object — plus the geo fields `cc_confidence`, `cc_source`, `asn`, `org`, and `uptime`, `feeders`,
and `city` (the city-cluster id, or `null`; never a coordinate). `verdict` is an
open token (e.g. `serves`), never validated — render an unknown one as plain
text. `state` decodes as an open string; `listening` means the peer accepts
inbound connections, other details unknown.

🪤 The traps these names exist to keep visible:

- `tier1()` is `tier=1`, **exact**. `maxTier(1)` is a *ceiling* and also returns
  tier 0. Neither is sugar for the other.
- `sentries()` is `label=sentry` — **not** `tier=0`. They are different
  questions and may give different answers.
- `notFull()` is `accepting=true`, which is exactly `connectable=yes`. **It is
  an estimate of whether the peer has room for another inbound peer — not a
  guarantee, and it can be wrong in both directions.**
- `mempoolServers()` is `serves_mempool=true`: peers whose `serves` is
  **unknown** (`null`) are excluded — unknown is not "does not serve". Use
  `servesMempool([true, null])` to include them.
- The geo argument is tagged — `{cc: […]}` or `{region: […]}`. **`region` is
  published only for cloud-hosted peers** (`provider:region`, the cloud
  provider's region) and answers `400 filter-unavailable` while no geo
  data is loaded, which this client raises rather than swallowing into an empty
  list. `cc` is the broad geo key.
- `connectable` is **three-state and never folded**: `unknown` is not `no`.
- Clients send **no** `sort` unless you set one, so the server's default governs.
- `basePath` (default `''`, the `DEFAULT_BASE_PATH` constant) prefixes every
  route — only for a proxy that mounts the API under a prefix.

## Network map

See the network map: <https://hypervisor.gg/network> — the live graph these endpoints serve.

```ts
const graph = await client.networkMapGraph({ maxTier: 2, connectable: ['yes'] });
if (!('notModified' in graph) && graph.envelope.available) {
  console.log(graph.nodeCount, graph.edgeCount, graph.tiers);
  for (const e of graph.edges) console.log(e.from, '->', e.to, e.span);
  for (const n of graph.nodes) console.log(n.id, n.auction.status); // same Peer type
}
const summary = await client.networkMapSummary();
if (!('notModified' in summary)) {
  console.log(summary.connectable, summary.servesMempool, summary.auction); // bucket counts
  // city clusters: null when the producer serves no city positions (not [])
  for (const c of summary.cities ?? []) console.log(c.id, c.lat, c.lon, c.peers);
  if (summary.attribution) console.log(summary.attribution); // show it with the map
}
```

Options: `maxTier`, `showUnreached`, `hideUnreachable`, `connectable` (closed
enum), `cc`, `operator`, `label`, and `servesMempool` / `auction`, typed
and validated exactly like the builder's. Same ETag cache, errors, `rateLimit` and
`apiVersion` as every other call; `available: false` is returned as-is.

## Gossip config from the API

`listPeersAsGossipConfig` asks the server for its `format=gossip_config`
rendering and hands back a parsed `GossipConfig` plus the response metadata. It
never writes; `save()` does, through the one safe write path.

```ts
import { save } from 'hl-peers/gossip-fs';

const res = await client.listPeersAsGossipConfig(peerQuery().cc(['DE']).tryNewPeers(true));
if (!('notModified' in res)) {
  console.log(res.config.toJson());                          // canonical §6.3 bytes
  save(res.config, '/home/hl/override_gossip_config.json');  // validate, atomic, 0600
}
```

`listPeers` refuses `format('gossip_config')` and names this helper: that body
is a config file, not a peer list.

## Gossip-config recipes

> 🔴 **The node reads `override_gossip_config.json` when it
> starts.** Writing this file does **not** steer a running node: the change takes
> effect at the node's next start. This library will **never** restart, signal,
> or otherwise disturb your node — that is your decision, at your moment.
> Restarting a node *to apply a config* while it is behind can make a lagging
> box worse.
>
> The node's file is `$HOME/override_gossip_config.json`.

```ts
import { GossipConfig, tier1 } from 'hl-peers';
import { load, save, updateGossipConfig, defaultConfigPath } from 'hl-peers/gossip-fs';

// build from a query and print it — the safest path: you place the file
const { json } = await updateGossipConfig(client, tier1().accepting(true), undefined, {
  chain: 'Mainnet',   // 🔴 never guessed; a wrong chain stops the node from starting
  maxRoots: 8,
});

// update a file in place, pinning peers you chose by hand
const res = await updateGossipConfig(client, tier1().accepting(true), defaultConfigPath(), {
  maxRoots: 8,
  keepExisting: ['192.0.2.10', '192.0.2.11'], // always retained, placed first
  exclude: ['198.51.100.9'],                  // 🪤 exclude beats keepExisting
  backup: true,
});
for (const w of res.warnings) console.warn(w.code, w.message);

// see what would change, and write nothing
const { diff } = await updateGossipConfig(client, tier1(), defaultConfigPath(), { dryRun: true });

// offline algebra — no network, no clock, no environment
const cfg = load(defaultConfigPath());
cfg.addRootIp('203.0.113.42');        // a duplicate add is a no-op
cfg.removeRootIp('203.0.113.9');      // removing an absent IP is a no-op
cfg.setReservedPeerIps(['198.51.100.20']); // 🪤 bare strings, unlike root_node_ips
cfg.setTryNewPeers(false);
cfg.setNGossipPeers(20);              // refused outside [8, 100], never clamped
const { warnings } = cfg.validate();  // state warnings ∪ pending merge warnings
save(cfg, defaultConfigPath(), { backup: true, fileMode: 0o600 });

// or from nothing at all
GossipConfig.fromPeers(['192.0.2.10'], { chain: 'Mainnet', tryNewPeers: false });
```

### The safety rules this library will not let you break

1. 🔴 **Never an empty `root_node_ips`.** hl-node asserts a non-empty root list
   at child start; an empty list is a crash loop with no built-in fallback. So
   `validate()`, `save()`, `mergePeers()` and `fromPeers()` all refuse it — and
   when your filter matches nothing, `updateGossipConfig` **raises** rather than
   emptying your file or silently keeping your old roots. Widen the filter, or
   pass `keepExisting`.
2. 🔴 **`available: false` never becomes an empty peer set** in a config path.
   It raises `UnavailableError` before any file is touched.
3. **Two entry shapes in one file.** `root_node_ips` entries are
   `{"Ip": "…"}` objects — capital `I`, lowercase `p`, no port field.
   `reserved_peer_ips` entries are bare strings. Both are pinned by fixtures.
4. **`maxRoots` defaults to 8**, not the upstream 24: a short root list makes
   the node start faster, and a longer one buys nothing. Pass `maxRoots: 24`
   for the upstream default; above 24 you get a warning, above 64 an escalated one.
5. **IPv6 is refused**, explicitly, because the node's support for it here is
   *unverified* and the failure mode is a crash loop.
6. **Unknown keys survive** a load → merge → save untouched, re-emitted after
   the known keys, sorted.
6b. **Warnings are never lost and never swallowed.** The six codes are
   **independent** — 25 roots with `try_new_peers: false` reports *both*
   `many_roots_gt_24` and `try_new_peers_false_multi_root`; only
   `many_roots_gt_24` / `many_roots_gt_64` are mutually exclusive. A merge's
   `truncated` / `exclude_overrides_keep_existing` stay **pending on the
   config** and are re-reported by `validate()`, `save()` and
   `updateGossipConfig()` (deduplicated by code), replaced by the next merge and
   dropped only by `clearWarnings()` — a successful `save` does not clear them.
7. **`save()` is atomic and idempotent**: validate → render → compare → if
   identical, do nothing at all (no write, no `.bak`, no mtime change) →
   otherwise temp file in the same directory, `fsync`, rename; the existing file
   mode is preserved and a new file is `0600` unless `fileMode` says otherwise.
   🔴 The permission option is **`fileMode`**, never `mode` — `mode` is the
   merge strategy (`replace` | `append` | `prepend`).

## Rate limits, caching, anonymous vs key

Anonymous is a **first-class tier**, not an error: omitting a key is never a
`401`. A `401` means a key was supplied and is invalid.

Every response — `200`, `304` and every error — exposes the `RateLimit-*`
headers as `rateLimit: { limit, remaining, reset, policy, retryAfter }`, plus
`apiVersion` (the `API-Version` header, informational). A missing header is
`null`, never `0`, and `reset` is **seconds from now**, not an epoch.

A `429` raises `RateLimitedError` with `retryAfter`, `resetAt` (a `Date`:
now + `Retry-After`, else now + `RateLimit-Reset`, from the client's injectable
clock) and `signupUrl` (where to raise the limit):

```ts
try {
  await client.listPeers(tier1());
} catch (err) {
  if (err instanceof RateLimitedError) console.warn('wait until', err.resetAt, err.signupUrl);
  else throw err;
}
```

🔴 **Auto-retry is off by default** (`maxRetries: 0`): the library never sleeps
behind your back. Opt in with e.g. `new PeerApiClient({ maxRetries: 3 })`: then
a `429` honours `Retry-After` up to `maxRetryAfterMs` (default 60 s) — **above
the cap it raises immediately** rather than blocking you for minutes — and
`5xx` / transport faults get jittered exponential backoff. A `4xx` other than
`429` is **never** retried.

`Retry-After` is resolved per §5.2: all digits are seconds; anything else is
parsed as an IMF-fixdate against the client's **injectable clock**
(`max(0, date - now)`, a past date yielding `0`); a value that is neither is
treated as **absent**. `rateLimit.retryAfter` is always the resolved seconds,
and `rateLimit.retryAfterRaw` keeps the header.

🔴 The library never parses the human `detail` prose for numbers (CLIENT_SPEC
§10, D-A); the headers are the truth.

**Caching (ETag / `If-None-Match`)** is on by default: a 64-entry in-memory
LRU, per client instance, never global and never on disk. A repeated request
sends `If-None-Match`; the server's `304` is handled transparently — you get
the stored body with `fromCache: true` — and 🔴 **a `304` is free: it costs no
quota unit**, keyed or anonymous. That is why the cache is on and why polling
should be conditional. `pollPeers` refuses an interval below 60 s and prefers
`next_sweep_at` plus per-instance jitter. Pass your own `ifNoneMatch` to get a
`NotModifiedResult` instead.

## Errors

```ts
import {
  BadRequestError, ChainNotServedError, CursorExpiredError, DecodeError,
  FilterUnavailableError, InvalidError, NotFoundError, PeerApiError,
  RateLimitedError, ServerError, TransportError, TruncatedError,
  UnauthorizedError, UnavailableError,
} from 'hl-peers';
```

Every error extends `PeerApiError`, carries `kind`, `rateLimit`, `apiVersion`
and the raw `problem` document, and is discriminated on `problem.type` by suffix — never on
prose. `FilterUnavailableError`, `ChainNotServedError` and `CursorExpiredError`
extend `BadRequestError`, so catching the 400 family catches them too.

🔴 On `CursorExpiredError`, start the walk over deliberately if that is what you
want; the client never restarts it for you, because that would mix two graph
generations — exactly what the server refused to do.

## Runtime support

| runtime | core (`.`) | `…/gossip-fs` | `hl-peers` CLI |
|---|---|---|---|
| Node >= 18 | ✅ | ✅ | ✅ |
| Bun | ✅ | ✅ (Node-compatible `fs`) | untested |
| Deno | ✅ (global `fetch`) | ❌ (`node:fs`) | ❌ |
| Browsers / workers | ✅ (inject `fetch` if needed) | ❌ | ❌ |
| Cloudflare Workers | ✅ | ❌ | ❌ |

The core imports nothing from `node:*`. `fetch`, the clock, the PRNG and
`sleep` are all injectable.

## CLI

```bash
hl-peers query --chain Mainnet --cc DE --output ips
hl-peers query --serves-mempool true --country DE --output ips
hl-peers query --auction holds_slot,bidding --output table
hl-peers query --cc DE --format gossip-config > override_gossip_config.json
hl-peers facets --serves-mempool true,null
hl-peers peer 192.0.2.10
hl-peers gossip show     --path ~/override_gossip_config.json
hl-peers gossip validate --path ~/override_gossip_config.json
hl-peers gossip update   --path ~/override_gossip_config.json \
    --tier 1 --accepting --max-roots 8 --keep 192.0.2.10 \
    --file-mode 0600 --backup --dry-run
hl-peers gossip update --cc DE --max-roots 8 --stdout \
    > override_gossip_config.json
```

Exit codes: `0` ok · `1` client/validation · `2` usage · `3` rate limited ·
`4` unauthorized · `5` not found · `6` unavailable/server/transport.

🔴 There is no `--restart` and no `--apply`. After a successful write the CLI
prints, to stderr: *"The node reads this file when it starts — the
change takes effect on the node's next start, which is yours to schedule."*

## Development

```bash
npm install
npm run typecheck   # tsc --noEmit, strict
npm run lint        # eslint flat config + typescript-eslint (type-aware)
npm test            # node:test over test/*.test.ts — no network
npm run build       # dist/esm + dist/cjs + .d.ts
npm run docs:api    # TypeDoc into docs-api/ (gitignored)
npm run check       # all of the above
```

Tests run on Node's own runner with **native TypeScript type stripping** (Node
>= 22.6), so the suite needs no transpiler and no test framework. The library
itself supports Node >= 18.

`test/conformance.test.ts` is **one generic runner** over all 76 cases in
`../fixtures/gossip/cases`, byte-exact, asserting the warning set from a final
`validate()`. HTTP behaviour is replayed through an injected `fetch` over all
38 `../fixtures/http` cases, and one integration test spawns
`tools/mock_server.py` on a free port. `test/fields.test.ts` reads
`../spec/peer-api-v1.fields.json` and asserts the hand-shaped decoders cover
exactly the spec's field sets. No default test run touches the network.

### Notes

1. **Warning codes are independent** (§6.4): every condition that holds fires,
   and only `many_roots_gt_24` / `many_roots_gt_64` are mutually exclusive.
2. **An explicit `apiKey: null` beats `HL_PEER_API_KEY`** — it is how you say
   "call anonymously" from code.
3. `engines.node` is `>=18`, and the tests use `node:test`.

