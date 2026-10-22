# `tools/cross_check` — the differential test

Four independent implementations of one contract can each pass the shared
conformance suite and still disagree with each other, because a suite only pins
what someone thought to ask about.

This is the differential test. It generates inputs, drives all four clients
through them, and asserts the answers are identical: **byte for byte** on
rendered JSON, and equal on error kind, warning-code set, canonical request URL,
parsed peer list and rate-limit block.

```bash
python3 tools/cross_check/run.py                       # everything
python3 tools/cross_check/run.py --only gossip         # one family
python3 tools/cross_check/run.py --langs python,rust   # two clients
```

Exit status is 0 only when every client agrees on every programme. Pure stdlib;
it needs each language's toolchain and a built Rust `cross-check` binary, which
it builds itself.

## The pieces

| file | what |
|---|---|
| `generate.py` | the seeded input generator. `--seed` makes any disagreement reproducible |
| `run.py` | the driver: spawns the four harnesses, streams programmes, diffs answers |
| `<lang>/cross_check/` | one tiny JSON-in/JSON-out harness per client |

A harness holds **no expectations**. It reports what its client did; `run.py`
decides whether the four reports match. That division matters: a harness that
knew the right answer would quietly paper over the disagreement it exists to find.

## What is generated

- every `fixtures/gossip/cases/*.json` programme, replayed through the harnesses;
- odd-but-valid input JSON (whitespace, key order, `\u`-escapes, surrogate pairs)
  and malformed input;
- boundary root counts — 0, 1, 8, 24, 25, 64, 65 — as `new`, as `from_peers` and
  as `max_roots`;
- every invalid-IP and IPv6 spelling, as a candidate and as a root;
- the zero-roots refusal, three ways;
- **240 randomised operation sequences** over a spread of starting configs
  (unknown keys, nested unknown objects, non-ASCII, an empty key, a float);
- **202 query-builder canonicalisations**, including the client-side `Invalid`
  cases of §2.2 and the three named presets;
- one request per HTTP fixture against `tools/mock_server.py`, plus the 3-page
  cursor walk, the `max_items` guard, an ETag revalidation and two retry
  schedules.

## The protocol

NDJSON in, NDJSON out, one answer per request, in order. A harness must not exit
on a bad request; it answers `{"harness_error": "…"}` and reads the next line.

```jsonc
// gossip
{"kind":"gossip","id":"…","input_json_text":"{…}",   // or "input_config": {…}|null
 "operations":[{"op":"merge_peers","args":[["203.0.113.1"]],"opts":{"mode":"replace"}}]}
->
{"id":"…","ok":true,
 "json_text":"…",          // to_json() of the subject, or null
 "warnings":["…"],         // sorted validate() codes, or null when validate raised
 "validate_error":{…},     // only when validate() raised
 "op_warnings":["…"],      // codes from the last merge/from_peers/save/validate
 "value":…,                // the last operation's value, normalised
 "saved_text":"…"}         // the save target's bytes, after a save
->
{"id":"…","ok":false,"error":{"kind":"Invalid","parameter":"peers[1]","at_op":0}}
```

```jsonc
// query_url — the canonical URL, or the client-side Invalid
{"kind":"query_url","id":"…","query":{"cc":["DE","NL"],"min_uptime":0.95}}
-> {"id":"…","ok":true,"url":"https://api.hypervisor.gg/peers?cc=DE%2CNL&min_uptime=0.95"}

// http — one real request against a mock server the driver starts
{"kind":"http","id":"…","op":"list_peers","base_url":"http://127.0.0.1:…","query":{"tier":"1"}}
-> {"id":"…","ok":true,"status":200,"peer_ids":["…"],"envelope":{…},"rate_limit":{…},"etag":"…",
    "api_version":"1"}
// op is one of list_peers, list_peers_twice, iter_peers, get_peer, facets,
// network_map_graph, network_map_summary (report "peer_ids" = node ids and
// "edge_count"), and list_peers_as_gossip_config (report "json_text" = to_json()).
// "max_retries", when present, is passed to the client; otherwise the default (0).
```

**Report shapes (normative for every harness, so four answers compare):**

- `envelope` (peers endpoints) = `{version, available, stale, chain, generated_at,
  next_sweep_at, attribution}`.
- every successful `http` answer adds `"api_version"` (the `API-Version` header, or null) — **except `iter_peers`**, whose answer stays `{ok, peer_ids, truncated}` (a lazy stream has no single response). On a cache-served 304 it is the 304's own header (the mock keeps `API-Version` on synthesised 304s).
- `/network-map/*` string filters (`cc`, `operator`, `label`) are sent **exactly as given** — no upper-casing (§4.8 types them as plain strings).
- `list_peers` / `get_peer` add `"signals": [[id, mempool.serves, mempool.source,
  auction.status, auction.slot], …]` in peer order (nulls as JSON null).
- `network_map_graph` → `{ok, status, peer_ids (node ids in order), edges:
  [[from, to, span], …], tiers: [[tier, count], …], node_count, edge_count,
  unreached, truncated, operator_nodes_count, envelope: {version, available, stale,
  generated_at}, api_version, rate_limit, etag, from_cache}`.
- `network_map_summary` → the same **without** `peer_ids`, `edges`, `truncated`,
  **plus** `connectable: {bucket: count}`.
- `list_peers_as_gossip_config` → `{ok, status, json_text (to_json() of the
  returned config), api_version, rate_limit, etag, from_cache}`.
- a `RateLimited` error payload adds `"reset_at"` (`YYYY-MM-DDTHH:MM:SSZ`) **only
  when the request carried `clock_now`**, and `"signup_url"` always (null if absent).
- `"max_retries"` on a request is passed to the client; absent ⇒ the client default (0).

- `list_peers` / `get_peer` add `"rows": [[id, state, tier, city], …]` in peer
  order (nulls as JSON null) — `state` as decoded (open enum, so `listening` and
  any unknown member pass through), `city` the cluster id or null.
- `network_map_graph` adds `"node_cities": [city, …]` (one per node, in
  `peer_ids` order), `"cities": [[id, name, cc, lat, lon, peers], …]` in server
  order or **null when the key is absent**, and `"attribution"` (null when absent).
- `network_map_summary` adds `"serves_mempool": {bucket: count}`,
  `"auction": {bucket: count}` (empty object when absent), and the same `"cities"`
  and `"attribution"` as the graph.
- `/network-map/*` take `serves_mempool` and `auction` exactly as the `/peers`
  builder does (list or pre-joined CSV, closed enums, `null` member allowed);
  an out-of-enum member is the client-side `Invalid{parameter}` before any request.
  Harnesses accept the fixture's pre-joined strings for these two.

Rules the harnesses share:

- `$TMPDIR` in any argument becomes a fresh per-request directory, and is mapped
  **back** to the literal `$TMPDIR` in every string reported, so four different
  temp paths do not read as four different answers.
- `error.kind` is the `CLIENT_SPEC.md` §5.1 taxonomy name, not the language's own
  spelling. Keys whose value is null are omitted.
- `status` is **not** reported on an error: §5.1 puts it on the `Server` variant
  alone, so exposing it elsewhere is a per-client accident.
- The client is built with a no-op sleeper,
  jitter fixed, and no access to the real `HL_PEER_API_*` environment.

## Why the driver runs its own mock servers

`X-Mock-Scenario` is a mock affordance; no real client sends it, so a
scenario-only fixture is unreachable over the wire. For each HTTP programme the
driver starts `tools/mock_server.py` over a **temporary fixture directory holding
just that fixture**, with its `request.headers` stripped — which makes the
response the only match. A programme with `fail_first_n` gets a **fresh server per
client**, because that counter is server-side and one shared instance would hand
the first client every injected failure and the next a clean `200`.

Delays are never asserted: `CLIENT_SPEC.md` §5.2 says the jitter source is not
required to match across languages. What is asserted is the *decision* — which
statuses retry, and what the caller finally gets.

## When it fails

`run.py` prints the programme id and, per key, what each client answered. Decide
from `CLIENT_SPEC.md` which is right; where the spec is silent, decide, **write
it into the spec**, add a case to `tools/gen_fixtures.py` so the shared suite
pins it too. A fix that lives only here is a fix the next change will undo.

- **`network` (testnet, beta; `CLIENT_SPEC.md` §2.1, §4.3, §4.4, §4.8).** Every
  `http` op passes the programme's `query.network` (when present) to the client:
  as the builder field on `list_peers` / `iter_peers` / `facets` /
  `list_peers_as_gossip_config`, as the per-call option on `get_peer` and
  `/network-map/*`. A value outside `mainnet` | `testnet` is the client-side
  `Invalid{parameter: "network"}` before any request, on every op. `query_url`
  takes `network` like any other builder field.
- `envelope` (peers endpoints and `/network-map/*`) adds `"network"` (the
  body's `network`, or null when absent — every mainnet body).
- `list_peers` / `get_peer` `"rows"` become `[id, state, tier, city, version]`;
  `network_map_graph` adds `"node_versions": [version, …]` in `peer_ids` order.
  `version` is the decoded string or null.
