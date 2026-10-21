# TypeScript client

`hl-peers` **1.1.0** — a client for the Peer API at
`https://api.hypervisor.gg` and an offline `override_gossip_config.json` file
algebra, with zero runtime dependencies.

> Install from a local build of this repository; an npm release will follow. Every example on this page also runs against
> `python3 tools/mock_server.py`, with no live host and no API key.

Normative behaviour: [`../CLIENT_SPEC.md`](../CLIENT_SPEC.md). Where this page
disagrees with it, the spec wins and this page is a bug. Background:
[concepts.md](concepts.md) and [gossip-config.md](gossip-config.md).

---

## Install and run

```bash
# 🪤 The published surface is `dist/`, and a fresh clone has none. Build it once.
npm --prefix /path/to/hl-peers/typescript ci
npm --prefix /path/to/hl-peers/typescript run build
npm install file:/path/to/hl-peers/typescript
```

```bash
# against the mock server instead of the live host
# terminal 1
python3 tools/mock_server.py --port 8931

# terminal 2
export HL_PEER_API_BASE_URL=http://127.0.0.1:8931
node --experimental-strip-types quickstart.ts     # Node >= 22.6 runs .ts directly
```

Requirements: Node >= 18 for the library (global `fetch`); Node >= 22.6 only if
you want to run `.ts` sources without a build step, as this repo's test suite
does.

## Entry points

| specifier | what it holds | runtime |
|---|---|---|
| `hl-peers` | `PeerApiClient`, `PeerQuery` and presets, the error classes, `GossipConfig`, the serializer | anything with `fetch` — Node, Bun, Deno, browsers, workers |
| `hl-peers/gossip-fs` | `load`, `loadOrNew`, `save`, `defaultConfigPath`, `updateGossipConfig` | **Node only** (`node:fs`) |
| `hl-peers` (bin) | the CLI of CLIENT_SPEC §8 | Node only |

The split is deliberate: the core imports nothing from `node:*`, so a browser
bundle that only queries the API never pulls in the filesystem.

ESM and CJS are both published, with `.d.ts` for each:

```ts
import { PeerApiClient } from 'hl-peers';           // ESM
const { PeerApiClient } = require('hl-peers');       // CJS
```

## The client

```ts
new PeerApiClient({
  baseUrl,                 // argument -> HL_PEER_API_BASE_URL -> https://api.hypervisor.gg
  basePath: '',            // route prefix (DEFAULT_BASE_PATH); plain paths at the root
  apiKey,                  // argument -> HL_PEER_API_KEY -> null (anonymous)
  timeoutMs: 10_000,       // per attempt, connect + read
  maxRetries: 0,           // 🔴 retries OFF by default; set e.g. 3 to opt in
  maxRetryAfterMs: 60_000, // with retries on, above this a 429 raises instead of sleeping
  minPollIntervalMs: 60_000,
  cache,                   // EtagCache | null; default 64-entry in-memory LRU
  userAgent, userAgentSuffix, // default user agent: hl-peers-ts/1.1.0
  fetch, now, random, sleep,  // all injectable, for tests and runtimes
  env,                     // defaults to process.env where one exists
});
```

`suppressPlaceholderWarning` and `logger` are accepted and have no effect.

An explicit `apiKey: null` means "call anonymously" and beats the environment
variable. The key is sent as `Authorization: Bearer …` and never appears in a
log line, a query string or an error message.

`networkMapGraph` / `networkMapSummary` below serve the live peer graph. See the
network map: <https://hypervisor.gg/network>.

| method | answers |
|---|---|
| `listPeers(query?, opts?)` | one page: `PeerListResult \| NotModifiedResult` |
| `iterPeers(query?, opts?)` | `AsyncIterable<Peer>` with `truncated`, `seen`, `envelope` |
| `listAllPeers(query?, opts?)` | eager convenience, same guard |
| `getPeer(ip, opts?)` | one peer's public dossier |
| `facets(query?, opts?)` | discovery over every filter; `isAvailable(name)`, `geoCoverage` |
| `listPeersAsGossipConfig(query?, opts?)` | the server's `format=gossip_config` rendering as a `GossipConfig` + metadata; never writes |
| `networkMapGraph(options?, opts?)` | `/network-map/graph`: `nodes` (as `Peer`), `edges`, `tiers`, counts, `cities` / `attribution` (null when absent) |
| `networkMapSummary(options?, opts?)` | `/network-map/summary`: counts, `tiers`, `connectable` / `servesMempool` / `auction` buckets, `cities` / `attribution` |
| `pollPeers(query, intervalMs, cb, opts?)` | conditional polling aligned to `next_sweep_at` |

Per-call options: `signal` (AbortSignal), `ifNoneMatch`, `deadlineMs`, and for
`iterPeers` also `maxItems` (default **10 000**, never unbounded) and
`onTruncate: 'stop' | 'raise'`.

Every successful result carries `envelope`, `rateLimit`, `etag`, `fromCache`,
`status` and `apiVersion` (the `API-Version` header; informational, never
validated). Nothing about the response is hidden. The envelope adds
`attribution` (the geo database's credit line).

```ts
const res = await client.listPeers(tier1());
if ('notModified' in res) { /* you asked the conditional question yourself */ }
else {
  res.envelope.available;   // 🔴 check BEFORE `stale`
  res.envelope.next_sweep_at;
  res.rateLimit.remaining;  // null when the header was absent, never 0
  res.nextCursor;
}
```

### Queries

`PeerQuery` is immutable: every setter returns a new query. Serialization is
lexicographic by parameter name, CSV lists join with `,` and the comma is
percent-encoded like anything else, so two clients asking the same question
produce the same URL and the same CDN cache key.

```ts
peerQuery().cc(['de', 'nl']).tier([1, null]).sort('random').limit(50).toQueryString();
// cc=DE%2CNL&limit=50&sort=random&tier=1%2Cnull
```

Client-side validation is deliberately thin (CLIENT_SPEC §2.2): only
`seen_within`'s pattern, `min_uptime`'s range, negative ceilings, `limit < 1`,
`max_latency_ms` without `vantage`, and the **closed** enums. `cc`, `region`,
`asn`, `role`, `label`, `version` and `fields` are open sets and are sent as
given — a server `400` that names the problem beats a client guess.

The builder also has `country()` (server-side alias of `cc`), `servesMempool()`
over `true`/`false`/`null`, `auction()` over `holds_slot`/`bidding`/`bid_before`/`null`
and `tryNewPeers()`; `serves_mempool`, `auction` and `state` are closed enums
checked locally. The network map takes `servesMempool` / `auction` options with
the same types and validation as the builder.

```ts
peerQuery().servesMempool([true, null]).auction(['holds_slot']).toQueryString();
// auction=holds_slot&serves_mempool=true%2Cnull
```

Presets: `tier1()`, `sentries()`, `notFull({cc|region})`, `mempoolServers()`.
"Serves the mempool and in DE" is `mempoolServers().cc(['DE'])`; 🔴 it excludes peers with
`mempool.serves === null` (unknown), because "unknown" is not "does
not serve". `region` is published only for cloud-hosted peers. See the README
for the traps each preset keeps visible.

### Peers

Decoded fields keep the wire's snake_case. Beyond the directory basics, each
`Peer` carries `cc_confidence`, `cc_source`, `asn`, `org` (open strings),
`uptime`, `feeders`, `city` (1.1.0: city-cluster id or `null`), and two typed objects: `mempool` (`serves`,
`last_checked`, `verdict`, `first_frame_ms`, `source`) and `auction` (`status`,
`slot`, `since`, `last_bid_at`). All-`null` means unknown; a key the server
omits decodes to the all-`null` object; unknown top-level keys land in
`extra`. `test/fields.test.ts` checks these sets against
`spec/peer-api-v1.fields.json` at test time.

### Errors

| class | raised on | `kind` |
|---|---|---|
| `RateLimitedError` | `429` | `RateLimited` |
| `FilterUnavailableError` | `400 …/filter-unavailable` | `FilterUnavailable` |
| `ChainNotServedError` | `400 …/chain-not-served` | `ChainNotServed` |
| `CursorExpiredError` | `400 …/cursor-expired` | `CursorExpired` |
| `BadRequestError` | any other `400` | `BadRequest` |
| `UnauthorizedError` | `401` | `Unauthorized` |
| `NotFoundError` | `404`, and a missing local config file | `NotFound` |
| `UnavailableError` | `503`, and `available:false` in a config path | `Unavailable` |
| `ServerError` | any other `5xx` | `Server` |
| `TransportError` | timeout, DNS, TLS, reset, abort | `Transport` |
| `DecodeError` | not JSON, or not the schema | `Decode` |
| `InvalidError` | client-side validation | `Invalid` |
| `TruncatedError` | `iterPeers` hit `maxItems` with `onTruncate: 'raise'` | `Truncated` |

The three named 400s extend `BadRequestError`. Discrimination is on
`problem.type` by suffix; a non-problem body falls back to the status alone
(D-K) with `problem: null` and a ≤ 512-byte body excerpt.

`RateLimitedError` carries `retryAfter`, `resetAt` (a `Date`: now +
`Retry-After`, else now + `RateLimit-Reset`, from the injectable clock),
`limit`, `remaining`, `reset`, `policy` and `signupUrl`. Every error carries
`apiVersion`.

🔴 **Retries are off by default** (D-Q): one `503` is one request, one `429`
raises at once. With `maxRetries > 0`, `429` / `5xx` / transport failures are
retried as §5.2 describes.

A `304` is handled transparently from the ETag cache (`fromCache: true`) and
**costs no quota unit**.

**`Retry-After` (§5.2)** is resolved for you: all digits ⇒ seconds; otherwise an
IMF-fixdate converted against the **injectable clock** (`max(0, date - now)`, a
past date yielding `0`); neither ⇒ **absent**, falling back to backoff. The
resolved value is `rateLimit.retryAfter`; the raw header stays on
`rateLimit.retryAfterRaw`. The `maxRetryAfterMs` cap applies to both forms, so
an HTTP-date an hour out raises `RateLimitedError` rather than blocking.

## GossipConfig

Pure, offline, deterministic: no network, no clock, no environment lookups, and
🔴 no restart of anything.

```
parse · new/create · fromPeers · clone · toJson · toJsonValidated · toObject
rootIps · hasRootIp · addRootIp · removeRootIp · setRootIps
reservedPeerIps · addReservedPeerIp · removeReservedPeerIp · setReservedPeerIps
chain · setChain · tryNewPeers · setTryNewPeers · nGossipPeers · setNGossipPeers
splitClientBlocks · setSplitClientBlocks · unsetSplitClientBlocks
validate · warnings · mergePeers
```

`mergePeers` / `fromPeers` return `{config, warnings}`; `validate()` returns
`{ok: true, warnings}` or throws the **first** error in CLIENT_SPEC §6.4's
order. Warning codes are the closed set: `many_roots_gt_24`, `many_roots_gt_64`,
`chain_absent`, `try_new_peers_false_multi_root`, `truncated`,
`exclude_overrides_keep_existing`.

🔴 **The codes are independent** (§6.4): every condition that holds fires, so 25
roots with `try_new_peers: false` reports both `many_roots_gt_24` and
`try_new_peers_false_multi_root`. The only mutually exclusive pair is
`many_roots_gt_24` / `many_roots_gt_64`.

**Merge warnings are pending state** (§6.5): `truncated` and
`exclude_overrides_keep_existing` arise during the merge, not from the resulting
document, so `mergePeers`/`fromPeers` both return them *and* retain them on the
config. `validate()`, `save()` and `updateGossipConfig()` report the union with
the state-derived warnings, de-duplicated by code. The next merge replaces them,
`clearWarnings()` drops them, and 🔴 a successful `save` does **not** clear them
— `save(); validate()` reports the same set twice. `pendingWarnings()` exposes
them directly.

Serialization is byte-exact and matches the Elixir, Rust and Python clients:
fixed key order (`chain`, `root_node_ips`, `try_new_peers`,
`reserved_peer_ips`, `n_gossip_peers`, `split_client_blocks`, then unknown keys
sorted by code point), 2-space indent, `": "` after each key, empty arrays
inline as `[]`, literal UTF-8, exactly one trailing `\n`. `JSON.stringify` does
not produce this; `renderGossipConfig` does.

The filesystem side (`…/gossip-fs`) adds `load`, `loadOrNew`, `save`,
`defaultConfigPath` and `updateGossipConfig`. §6.1 explicitly allows this split
for a language whose core must stay filesystem-free. 🔴 Their permission option
is **`fileMode`** (`--file-mode` on the CLI); `mode` is the merge strategy. See
[gossip-config.md](gossip-config.md) for the file itself and the four rules, and
the package README for the recipes — all of which are executed by the test
suite.

## Conformance and tests

```bash
cd typescript
npm install
npm run check    # typecheck + lint + test + build
```

- `test/conformance.test.ts` — **one generic runner** over all **76** cases in
  `fixtures/gossip/cases`, byte-exact, `$TMPDIR` substituted per case, with the
  warning set taken from a final `validate()` as `fixtures/README.md` requires.
- `test/http.test.ts` — all **38** fixtures: every endpoint incl. the network
  map, the four (single-page) common queries plus "mempool and DE"
  (`serves_mempool=true&cc=DE`), `serves_mempool=null`, `auction=holds_slot,bidding`,
  the `format=gossip_config` **golden** (`toJson()` and saved bytes),
  `filter-removed` and mempool `filter-unavailable`, `API-Version`, retries off
  by default, `resetAt` from an injected clock, the 3-page cursor walk,
  `304`/ETag, all four `429`s
  (numeric, over-cap, HTTP-date against an injected clock, unparseable — each
  asserted against the fixture's own `expect.retry_after_seconds`), `400`
  filter-unavailable, `401`, `404`, `500` non-problem, `503`, `available:false`
  and `stale:true` — through an injected `fetch`.
- `test/retry.test.ts` — retry **decisions** only; the clock, PRNG and sleep are
  injected and no wall-clock delay is ever asserted.
- `test/gossip-fs.test.ts` — atomic write, mode preservation, `.bak`, `dryRun`,
  the zero-roots refusal leaving an existing file untouched.
- `test/integration.test.ts` — spawns `tools/mock_server.py` on a free port,
  including `--fail-first-n` for the retry path.
- `test/examples.test.ts` — runs every documented sample in `typescript/examples`.
- `test/cli.test.ts` — the `hl-peers` exit codes, its stderr contract, and the
  `--serves-mempool` / `--auction` / `--country` flags.
- `test/fields.test.ts` — decoders and builder vs `spec/peer-api-v1.fields.json`.

No default test run touches the network.

Generated API reference: `npm run docs:api` writes TypeDoc HTML into
`typescript/docs-api/` (gitignored). The TSDoc comments in `typescript/src` are
the source of truth for the per-method traps.

## Warning codes

The `validate()` warning codes are independent: every condition that holds
fires, and only `many_roots_gt_24` / `many_roots_gt_64` are mutually exclusive.
