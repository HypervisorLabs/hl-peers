# CLIENT_SPEC — normative behaviour for every hl-peers client

**Status:** normative. **Version:** 1.1.0 + Unreleased (`network`, §10 D-U), tracking the live API:
`spec/peer-api-v1.openapi.yaml` (a redacted copy of the producer's public
document — see `spec/SOURCE.md`).

This document is the contract *between the four clients*. Elixir, TypeScript,
Rust and Python each own one directory and must converge on this behaviour. Where this document and the OpenAPI copy disagree, **this
document wins for client behaviour** and the disagreement is recorded in
§10 "Resolved ambiguities".

Keywords **MUST**, **SHOULD**, **MAY** are RFC 2119.

> 🔴 **Tests never touch the network.** Every client is built and tested against
> `fixtures/` and `tools/mock_server.py`. The API is live at the default base URL;
> the default test run must not need it.

---

## 0. Naming, and what "identical" means

Each language uses its own idiomatic casing and error mechanism. Identical means:

- **identical observable behaviour** — same request for the same query, same error
  variant for the same response, same retry decisions;
- **byte-identical gossip-config output** for the same input (§6.3). This is
  mechanically checked by `fixtures/gossip/cases/`;
- **the same method names, transliterated.** `list_peers` → `listPeers` in
  TypeScript, `list_peers/2` in Elixir, `list_peers` in Rust/Python. Do not rename.

Two namespaces, always separate:

| namespace | nature | needs network? |
|---|---|---|
| `Client` (§2–§5) | the HTTP query surface | yes |
| `GossipConfig` (§6) | pure, offline, deterministic file algebra | **never** |

`GossipConfig` **MUST NOT** depend on the HTTP layer. An operator must be able to
use it to edit a config file on a box with no API access at all. Only the one
high-level helper in §7 joins them.

---

## 1. Client configuration

| option | type | default | notes |
|---|---|---|---|
| `base_url` | string | `https://api.hypervisor.gg` | the public API host |
| `base_path` | string | `""` (none) | route prefix for every call; see below. The public API uses plain paths at the host root |
| `api_key` | string \| null | `null` | absent ⇒ anonymous, a **supported tier, not an error** |
| `timeout` | duration | `10 s` | total per attempt, connect + read |
| `user_agent` | string | `hl-peers-<lang>/<ver>` | `<lang>` ∈ `elixir`, `ts`, `rust`, `python` |
| `max_retries` | integer | **`0`** | attempts after the first. 🔴 **Auto-retry is OFF by default**; set e.g. `3` to opt in. See §5.2 |
| `min_poll_interval` | duration | `60 s` | guard on the polling helper only (§4.6) |
| `cache` | ETag cache \| null | in-memory, 64 entries, LRU | §4.5 |
| `user_agent_suffix` | string \| null | `null` | appended after a space, for downstream apps |
| `network` | enum `mainnet` \| `testnet` \| null | `null` | the default `network` (§2.1) for every call that does not set its own. `null` sends nothing, so the server's default (mainnet) governs. **Testnet (beta)**, §10 D-U |

**`base_url` resolution order**, evaluated once at client construction:
`explicit argument` → `HL_PEER_API_BASE_URL` → `https://api.hypervisor.gg`.
**`api_key` resolution order**: `explicit argument` → `HL_PEER_API_KEY` → `null`.

**`base_path`** is the prefix every route is built under: `list_peers` requests
`<base_url><base_path>/peers`, `get_peer` `<base_path>/peers/{ip}`, `facets`
`<base_path>/peers/facets`, and the network map `<base_path>/network-map/graph`
and `/network-map/summary`. The public API uses **plain paths at the host root**
(no `/api`, no `/v1`; the version is in every body and in the `API-Version`
header), so the default is the **empty prefix**, held in **exactly one**
constant per client. The option stays for a proxy that mounts the API under a
prefix. Normalization, identical in every client: strip every
leading and trailing `/`; an empty result is the empty prefix (routes at the host
root), anything else becomes `/` + the trimmed value. So `peer-api/v1/`,
`/peer-api/v1` and `//peer-api/v1//` all mean `/peer-api/v1`, and `/` and `""`
both mean "no prefix". Explicit argument only; there is no environment variable.

**`API-Version`.** The server sends `API-Version: 1` on every response, errors
included. Clients expose it as `api_version` (string, nullable) on every result
and every error, next to `rate_limit`. A client **MUST NOT** fail on a different
value — it is informational; a breaking change ships under `/v2` paths.

The key is sent as `Authorization: Bearer <key>`. A key MUST NOT be logged, put in
a query string, or included in an error message; redact it to `hlp_live_…` if a
diagnostic must mention it.

---

## 2. Query builder

A typed, immutable, chainable builder. Every setter returns a new value (or the
language's idiomatic equivalent); no setter mutates in place.

### 2.1 Fields

Exactly the parameters of `GET <base_path>/peers` (§1). CSV parameters take a **list** in
the client API and are joined with `,` at serialization; a client MUST NOT make the
caller build CSV by hand, but MUST accept a pre-joined string too.

| builder field | wire param | client type | wire form |
|---|---|---|---|
| `chain` | `chain` | enum `Mainnet` \| `Testnet` | as-is |
| `network` | `network` | enum `mainnet` \| `testnet` | as-is. Which network's graph to read; unset ⇒ omitted ⇒ mainnet. Falls back to the client's `network` option (§1). **Testnet (beta)**, §10 D-U |
| `cc` | `cc` | list of string | CSV, upper-cased |
| `region` | `region` | list of string | CSV |
| `asn` | `asn` | list of string | CSV |
| `tier` | `tier` | list of (integer \| the literal `null`) | CSV; absent member serializes as the string `null` |
| `max_tier` | `max_tier` | integer ≥ 0 | as-is |
| `role` | `role` | list of string | CSV |
| `label` | `label` | list of string | CSV |
| `country` | `country` | list of string | CSV, upper-cased. Alias of `cc` server-side (both given ⇒ union); a separate builder field so the URL says what the caller wrote |
| `connectable` | `connectable` | list of enum `yes`\|`no`\|`unknown` | CSV |
| `accepting` | `accepting` | boolean | `true` / `false` |
| `state` | `state` | list of enum `responsive`\|`open`\|`full`\|`syncing`\|`listening`\|`unreachable`\|`unknown` | CSV. `listening` = accepts inbound connections, other details unknown |
| `serves_mempool` | `serves_mempool` | list of (boolean \| the literal `null`) | CSV of `true`/`false`/`null`; `null` = unknown, **not** `false` |
| `auction` | `auction` | list of (enum `holds_slot`\|`bidding`\|`bid_before` \| the literal `null`) | CSV; `null` = never seen in the auction |
| `try_new_peers` | `try_new_peers` | boolean | `true`/`false`; only meaningful with `format=gossip_config` |
| `max_blocks_behind` | `max_blocks_behind` | integer ≥ 0 | as-is |
| `min_uptime` | `min_uptime` | number 0..1 | as-is |
| `max_latency_ms` | `max_latency_ms` | integer ≥ 0 | as-is |
| `vantage` | `vantage` | string | as-is |
| `seen_within` | `seen_within` | duration string `^[0-9]+[mhd]$` | as-is |
| `version` | `version` | list of string | CSV |
| `sort` | `sort` | enum `tier`\|`blocks_behind`\|`out_deg`\|`last_seen`\|`random` | as-is |
| `limit` | `limit` | integer ≥ 1 | as-is |
| `cursor` | `cursor` | string (opaque) | as-is |
| `fields` | `fields` | list of string | CSV |
| `format` | `format` | enum `json`\|`gossip_config` | as-is |

`tier` is **exact**; `max_tier` is a **ceiling**. They are different questions and a
client MUST NOT map one to the other. Both may be set at once; the server ANDs them.

### 2.2 Client-side validation — deliberately thin

A client **MUST** reject before sending, with `Invalid{parameter, reason}`, only:

1. `seen_within` not matching `^[0-9]+[mhd]$`;
2. `min_uptime` outside `[0, 1]`;
3. negative `max_tier`, `max_blocks_behind`, `max_latency_ms`; `limit` < 1;
4. `max_latency_ms` set without `vantage` (the spec says `vantage` is required then);
5. a value outside a **closed** enum — `chain`, `network`, `sort`, `format`, `connectable`,
   `state`, `serves_mempool` (`true`/`false`/`null`), `auction`
   (`holds_slot`/`bidding`/`bid_before`/`null`). Case matters.

A client **MUST NOT** validate anything else locally. In particular `cc`, `region`,
`asn`, `role`, `label`, `version` and `fields` are **open** sets that may grow
additively within v1; validating them locally would turn a server-side `400` that
names the problem into a client-side error that guesses at it. Send them and let the
server answer. `limit` is **not** clamped client-side: the server clamps, and
clamping locally would hide the tier ceiling from the caller.

### 2.3 Serialization

Parameters with a `null`/unset value are omitted entirely. An empty list is also
omitted (it is not `?cc=`). Ordering of query parameters in the emitted URL **MUST**
be lexicographic by parameter name, so that two clients producing the same query
produce the same URL and the same CDN cache key. Values are percent-encoded per
RFC 3986; the CSV `,` **MUST** be encoded (`%2C`) — do not special-case it.

### 2.4 Presets

Named presets. Each returns a query builder the caller may further refine.

```
tier1()                       -> tier: [1]
sentries()                    -> label: ["sentry"]
not_full(region|cc)           -> accepting: true, plus the geo argument
mempool_servers()             -> serves_mempool: [true]
```

- `sentries()` uses `label=sentry`, **not** `tier=0`. They are different
  questions and may diverge.
- `not_full()` is `accepting=true`, which is exactly `connectable=yes`. 🪤 Every
  client's docstring for `not_full` **MUST** state: *this is an estimate of whether
  the peer has room for another inbound peer, not a guarantee; it can be wrong in
  both directions.*
- "Serves the mempool AND in country X" is
  `mempool_servers().cc(["DE"])` (§10 D-O). The docstring **MUST** say that `serves: null` peers (unknown)
  are excluded, and that "unknown" is not "does not serve".
- The geo argument of `not_full` is a **tagged** argument, never
  a bare string: `Cc(["DE"])` or `Region(["aws:eu-central-1"])` (idiomatically: a keyword
  arg `cc=` / `region=`, an enum, or two named constructors). 🪤 `region` is
  published **only for cloud-hosted peers** (`provider:region`, the cloud
  provider's region) and answers `400 filter-unavailable` while no geo data is loaded;
  `cc` is the broad geo key. Each client's docstring for the geo argument MUST say so, and the
  `FilterUnavailable` error MUST NOT be swallowed into an empty list.

---

## 3. Response types

### 3.1 Envelope

Every successful body carries `version`, `available`, `stale`, `chain`, and
(except facets before the first data refresh) `generated_at`. Clients expose these on every result
object, and **MUST NOT** hide them.

🔴 `available: false` means *the producer could not build a graph*. The empty
`peers` list that comes with it is **not a finding about the network**. A client
**MUST NOT** convert `available: false` into an error, and **MUST NOT** let a caller
mistake it for "no peers matched". Two consequences:

- `list_peers` returns the envelope as-is with `available == false`.
- `iter_peers` **MUST** stop immediately on `available == false` without raising,
  and the caller can tell the difference by inspecting the flag.
- §6/§7: `from_peers` / `update_gossip_config` on an `available: false` result
  **MUST** raise `Unavailable{}` rather than build a zero-root config. See §7.

Check `available` **before** `stale`: a dark subsystem reports both as false.

`network` is present only on a `network=testnet` body (value `"testnet"`; decode as
an open, nullable string). Clients expose it on every result; absent ⇒ `null`, which
means mainnet. A testnet body also states `chain: "Testnet"`.

`next_sweep_at` is the data's own clock: when the data is next refreshed. Clients expose it and the polling helper
uses it (§4.6).

### 3.2 Peer

Decode every field in the OpenAPI `Peer` schema into a typed struct, plus an
`extra`/`unknown_fields` map holding any key the server adds later. Rules:

- `tier` is the distance from the network's sentry nodes in the peer graph (tier
  0 = sentries). `tier: null` means **the node was not placed**. It is **NOT** tier 0. A
  client **MUST NOT** default it to 0, to -1, or to a sentinel integer. In languages
  with no null integer, use the language's option type.
- `null` is always "not provided", never zero, never false.
- `connectable` is **three-state and never folded**. `unknown` means *we never
  observed this*; it is not `no`, not `false`. If a boolean convenience is offered
  it MUST be named for the positive confirmation only (`connectable?` ⇔ `== yes`)
  and documented as excluding `unknown`.
- `state`, `role` and `labels[]` are **open** enums/sets. An unrecognised member is
  decoded as-is and rendered plain — never an error, never dropped. (The *filter*
  `state` is closed client-side, §2.2; the *decoded* value is open.) Known states:
  `responsive`, `open`, `full`, `syncing`, **`listening`** (accepts
  inbound connections; its other details are unknown — typically `tier: null`),
  `unreachable`, `unknown`.
- **`city`** — nullable string: the id of the peer's city cluster
  (`<CC>-<city>`, e.g. `JP-tokyo`) when the producer serves city positions, else
  `null`. Never a coordinate. A server that omits the key decodes to `null`.
- `version` is a nullable string. On mainnet it is **`null`** (not published).
  On `network=testnet` (beta) it is the node's reported **hardfork version**
  (e.g. `"1550"`), `null` when the node did not report one. Decode it as an
  opaque string, never a number. `region` is set only for cloud-hosted peers.
  Documented, not asserted. Testnet rows carry the same geo/ASN fields as mainnet.
- Geo: `cc_confidence` (`high`|`medium`|`low`|null), `cc_source`
  (`cloud-range`|`db`|`db+rtt-confirmed`|null), `asn`, `org` — decode as open
  strings (unknown members pass through). `uptime` is a nullable number,
  `feeders` a list of IPv4 strings (possibly empty).
- 🔴 **`mempool`** — a typed sub-object `{serves: bool?, last_checked: datetime?,
  verdict: string?, first_frame_ms: int?, source: string?}` (`source` ∈ `probe` |
  `relay`, open: which of the producer's measurements produced the verdict).
  `verdict` is an **open** machine token (e.g. `serves`) — decode as a plain
  string, never validate it, render an unknown token as plain text.
  `first_frame_ms` is the time to the first mempool message. **`auction`** — `{status: string?, slot: int?, since:
  datetime?, last_bid_at: datetime?}` (`status` ∈ `holds_slot` | `bidding` |
  `bid_before`, open). Every published peer carries both; an all-`null` object
  means **unknown**. `serves: null` is never `false`. A peer from an older server
  that omits the key decodes to the all-`null` object, never to an error.
  Timestamps stay ISO-8601 strings where the language has no stdlib datetime the
  rest of the client already uses; otherwise parse them. Unknown keys inside these
  objects are ignored (they are not carried into `extra`).
- `connectable` wire values are the **strings** `"yes"`, `"no"`, `"unknown"` — not
  booleans. Decode to a three-valued type, never to `bool?`.

### 3.3 Rate-limit headers

Parsed from **every** response — 200, 304 and every error — into a
`RateLimit { limit, remaining, reset, policy, retry_after }` value exposed on the
result object *and* on the error. Anonymous 200s may carry only `RateLimit-Policy`
(shared-cacheable); the triple is then `null`, not 0. Each member is nullable; a missing header is
`null`, never 0.

- `RateLimit-Limit`, `-Remaining` → integer; `-Reset` → integer **seconds from now**
  (not an epoch); `-Policy` → the raw string, unparsed; `Retry-After` → integer
  seconds, per §5.2's HTTP-date rule.
- A non-integer or absent header is `null`. Never invent a value.

### 3.4 Facets

`facets()` returns `{envelope, total, facets: {name -> {available, values: [{value, count}]}}, geo_coverage}`.
Facet names today: `tier`, `cc`, `region`, `asn`, `role`, `label`, `state`,
`connectable`, `version`, `uptime`, `serves_mempool`, `auction` — an **open**
map; decode any name. `geo_coverage` is a nullable object, kept raw/loosely typed.
🔴 `available: false` on a facet means the attribute is **not collected**; an empty
`values` under `available: true` means it *is* collected and nothing matches. A
client **MUST** keep these distinguishable and **MUST NOT** flatten a facet to a
plain list of values without also exposing `available`. `value` is always a string,
including the selectable absent-value member, which is the literal `"null"`.

Clients SHOULD offer `facets().available?("asn")` so a caller can pre-check a filter
rather than discover `filter-unavailable` on a 400.

---

## 4. Query API

### 4.1 `list_peers(query) -> PeerList`

One page. Issues `GET <base_path>/peers` with the serialized query (§2.3).

🔴 `format = gossip_config` is **refused by `list_peers`**, client-side, with
`Invalid{parameter: "format"}`. That response body has no `peers` array — it is
the server's rendering of an `override_gossip_config.json` — so a `PeerList`
cannot be built from it. The two things a client could do instead are both worse:
raising `Decode` blames the server for a request the client should not have sent,
and returning a `PeerList` with `peers: []` hands the caller *the* mistake §3.1
exists to prevent, an empty peer list that is not a finding about the network.
A client that offers the format at all offers it as a **separate call**
(`list_peers_as_gossip_config`, transliterated), and the refusal's reason names
that call where it exists. Which clients offer it is recorded in the root
README's feature matrix; a client may offer none, and `update_gossip_config`
(§7) never needs it.

### 4.2 `iter_peers(query, opts) -> stream/iterator of Peer`

Cursor pagination to exhaustion. Lazy in every language that has laziness
(`Stream` in Elixir, generator in Python, `AsyncIterable` in TS, an `Iterator`/
`Stream` in Rust).

- Sends the first request with `query` as given; each subsequent request is
  `query` with `cursor` replaced by the previous page's `next_cursor`, everything
  else byte-identical.
- Terminates when `next_cursor` is `null`, when `peers` is empty, when
  `available == false`, or when `max_items` is reached.
- `opts.max_items` **defaults to 10 000** and is a hard guard: on reaching it the
  iterator stops and, if `opts.on_truncate == :raise`, raises `Truncated{seen}`;
  the default `:stop` ends the stream silently but sets a `truncated` flag on the
  iterator handle. A client **MUST NOT** default to unbounded.
- 🪤 A cursor is bound to the graph generation it was issued in. Paging across a
  data refresh answers `400 cursor-expired`. A client **MUST NOT** silently
  restart the walk on `cursor-expired` — that would mix two generations into one
  result set, which is exactly what the server refused to do. It surfaces
  `CursorExpired{}` (a `FilterUnavailable`-sibling variant of the 400 family, §5.1)
  and lets the caller decide. Clients **SHOULD** document "catch it and start over"
  as the intended handling.
- A client **MUST NOT** deduplicate across pages or reorder results.
- `iter_peers` is also the correct place to note: the whole directory is ~900 nodes,
  which is 18 anonymous pages at `limit=50`.

`list_all_peers(query, opts)` is an OPTIONAL eager convenience over `iter_peers`
with the same guard.

### 4.3 `get_peer(ip) -> PeerEnvelope`

`GET <base_path>/peers/{ip}`. `ip` is percent-encoded into the path. An optional
`network` (closed enum as §2.1, falling back to the client option) is the only query
parameter; unset ⇒ none is sent.

🔴 A malformed IPv4 is a `400`; a well-formed IPv4 that appears in no source is a
`404`. **These two MUST NOT collapse into one client error.** A client validates
IPv4 shape locally *before* sending and raises `Invalid{parameter: "ip"}` for a
non-IPv4, so a typo does not spend a quota unit — but it still maps a server `400`
to `BadRequest` and a `404` to `NotFound`, distinctly. IPv6 input is rejected
locally with a message saying the API is IPv4-only.

### 4.4 `facets(query?) -> Facets`

`GET <base_path>/peers/facets`. Accepts every **filter** of §2.1, `network` included, (the counts are
over the filtered set); passing `sort`, `limit`, `cursor`, `fields`, `format` or
`try_new_peers` is an `Invalid{parameter}` client-side, because the endpoint does
not take them and the caller would believe they applied.

### 4.5 ETag / conditional requests

A small in-memory cache, **on by default**, keyed on
`method + "\0" + path + "\0" + canonical_query_string + "\0" + (api_key_present ? "k" : "a")`.
The key includes whether a key was sent because the tier changes the page ceiling.

- On a cached hit, send `If-None-Match: <stored etag>`.
- On `304`: return the **stored body**, with `from_cache = true` on the result and
  the rate-limit headers from the **304 response** (not the stored ones).
- On `200` with an `ETag`: store `{etag, body, stored_at}`, evict LRU past `capacity`.
- A caller-supplied `If-None-Match` overrides the cache; the `304` is then returned
  as a distinct `NotModified` result rather than a body, because the caller asked
  the conditional question itself.
- `Cache-Control: max-age` is **not** honoured as a hard TTL — the client always
  revalidates. The cache exists to make polling free (a `304` costs no quota unit),
  not to serve stale bytes. Clients **MAY** offer `respect_max_age = true` as an
  opt-in.
- The cache is per-client-instance, never global, never on disk.

### 4.5a Rate limits and 304s are cheap by design

A `304` costs **no quota unit**, keyed or anonymous. Every README
says so next to the cache, because it is the reason the cache is on by default.

### 4.6 The polling helper

`poll_peers(query, interval, callback)` (name transliterated per language) is
OPTIONAL to *offer* but, if offered, MUST:

- refuse an `interval` below `min_poll_interval` (default 60 s) with
  `Invalid{parameter: "interval"}`, because the data refreshes on its own schedule
  and faster polling returns identical bytes and spends quota;
- use the ETag cache (§4.5), so unchanged polls cost no quota unit;
- prefer `next_sweep_at` when present: sleep until `next_sweep_at` plus a small
  jitter (5–15 s, randomised per client instance), falling back to `interval`.

---

### 4.7 `list_peers_as_gossip_config(query) -> GossipConfigResult`

**Required in all four clients.** Sends the query with
`format=gossip_config` (the caller's `format` is overridden; a caller-set
`try_new_peers` is sent). The body is parsed with `GossipConfig.parse` and returned
as a `GossipConfig` value plus the response metadata (`rate_limit`, `etag`,
`from_cache`, `api_version`, `status`). The helper does **not** write; the caller
writes with the normal `save(path, opts)` (§6.6: validate, atomic rename, mode
`0600`, optional `.bak`), so the one safe write path is reused. A client MAY offer
`path` / `write` sugar that calls `save` — never a second writer.

🔴 Golden test: for `fixtures/http/peers_format_gossip_config.json`, `to_json()` of
the returned config **and** the bytes `save()` writes **MUST** equal
`expect.gossip_config_json_text` byte for byte. The server emits `root_node_ips,
try_new_peers, chain`; the client re-renders in §6.3 order. The server never emits
an empty root list (it answers `400 empty-root-list`, a `BadRequest`).

### 4.8 `network_map_graph(opts)` / `network_map_summary(opts)`

The same graph, drawn live: [hypervisor.gg/network](https://hypervisor.gg/network).

`GET <base_path>/network-map/graph` and `/network-map/summary`. Options:
`max_tier` (int ≥ 0), `show_unreached` (bool), `hide_unreachable` (bool),
`connectable` (list, closed enum as §2.2), `cc`, `operator`, `label` (strings),
**`network`** (closed enum as §2.1, falling back to the client option),
and **`serves_mempool`** and **`auction`** — typed, serialized and
validated exactly as the §2.1 builder fields of the same name (list or pre-joined
CSV, closed enums with the literal `null` member; an out-of-enum member is
`Invalid{parameter}` before any request). Unset ⇒ omitted; lexicographic,
percent-encoded as §2.3. Same ETag cache, same error mapping, same
rate-limit/`api_version` exposure. Both endpoints are public: the anonymous tier
calls them without a key. Result types:

- graph: `{version, available, stale, network?, generated_at, operator_nodes: {count?},
  node_count, edge_count, tier_count, unreached, truncated, tiers: [{tier, count}],
  nodes: [Peer], edges: [{from, to, span?}], cities?: [CityCluster],
  attribution?}` — nodes decode with the §3.2 `Peer` decoder (so each carries
  `city`); `from` / `to` are published node ids (strings, not validated as IPv4);
- `CityCluster`: `{id, name, cc, lat, lon, peers, tiers: {tier-or-"null"
  -> count}}` — `lat`/`lon` are the city's own point rounded to 0.1°, the only
  coordinates in the API. `cities` and `attribution` are **absent** unless the
  producer serves city positions: a client keeps "absent" (`null`/`None`/`nil`)
  distinct from an empty list;
- summary: the same envelope without `nodes` / `edges` / `truncated`, plus
  zero-filled `connectable`, `serves_mempool` and `auction`
  `{bucket -> count}` maps (empty when absent), plus `cities` / `attribution` as
  on the graph (§10 D-S).

`available: false` is returned as-is, never an error (§3.1).

## 5. Errors, retries, and the request lifecycle

### 5.1 Error taxonomy

One closed set, transliterated into each language's idiomatic error mechanism
(tagged tuples in Elixir, a discriminated union/`Error` subclasses in TS, an enum in
Rust, an exception hierarchy in Python). Every error carries the parsed rate-limit
block (§3.3) and, where the body was problem+json, the raw problem object.

| variant | payload | raised on |
|---|---|---|
| `RateLimited` | `retry_after, reset_at, limit, remaining, reset, policy, signup_url, problem` | `429` |
| `FilterUnavailable` | `filter` (= `problem.parameter`), `detail`, `problem` | `400` whose `type` ends `/filter-unavailable` |
| `ChainNotServed` | `allowed`, `problem` | `400` … `/chain-not-served` |
| `CursorExpired` | `problem` | `400` … `/cursor-expired` |
| `BadRequest` | `parameter, allowed, detail, problem` | any other `400`, including `network-not-served` (§10 D-U) |
| `Unauthorized` | `detail, problem` | `401` |
| `NotFound` | `detail, problem` | `404` |
| `Unavailable` | `detail, problem` | `503`, **and** §3.1's `available: false` where a config would otherwise be built |
| `Server` | `status, body_excerpt` | any other `5xx` |
| `Transport` | `cause` | connect/read timeout, DNS, TLS, connection reset |
| `Decode` | `cause, body_excerpt` | body is not JSON, or does not match the schema |
| `Invalid` | `parameter, reason` | client-side validation (§2.2, §4.3, §4.4, §6.4) |
| `Truncated` | `seen` | `iter_peers` hit `max_items` with `on_truncate: :raise` |

`FilterUnavailable`, `ChainNotServed` and `CursorExpired` are **subtypes of the 400
family**: a caller catching `BadRequest` generically MUST also catch them. In
languages without subtyping, `BadRequest` carries a `kind` discriminator and the
three named variants are constructors over it.

`RateLimited.reset_at` is an absolute UTC instant, computed from the client's
injectable clock at response time: `now + retry_after` if `retry_after` is known,
else `now + reset` if `RateLimit-Reset` is known, else `null`. It is what a caller
sleeps until; nothing needs to parse a header. `signup_url` comes from the
problem body (`null` if absent).

**Discrimination is on `problem.type`**, by suffix match on the path segment, never
on `title` or `detail` (prose is not an API). If `type` is absent or unrecognised,
fall back to the status code alone. 🔴 A client **MUST NOT** parse the human
`detail` string for numbers — in particular the rate-limit figures quoted in the
`429` detail are prose and disagree with the published tier table (§10, D-A).

The per-address **flood** `429` (a burst limit applied before the tier limit) is
problem+json too: a `…/rate-limited` type, `Retry-After` in
seconds, its own `RateLimit-*` window and `Cache-Control: no-store`. It maps to
`RateLimited` exactly like the tier `429`; nothing distinguishes them but the
headers' values (`fixtures/http/error_429_ip_flood.json`).

A response that is not `application/problem+json` on an error status is still
mapped to the status's variant, with `problem = null` and a body excerpt of at most
**512 bytes**, truncated on a UTF-8 boundary.

### 5.2 Retry policy

🔴 **Off by default.** `max_retries` defaults to **0**: a `429` raises
`RateLimited` (with `reset_at`) and a `5xx` / transport failure raises at once,
so a library never sleeps behind a caller's back unless asked. With
`max_retries > 0`, that many additional attempts are made, applied only to:

| condition | delay |
|---|---|
| `429` | `Retry-After` seconds, **capped at `max_retry_after` (default 60 s)**. If `Retry-After` is absent or unparseable, fall back to the backoff below. If it exceeds the cap, **do not sleep the cap and retry** — raise `RateLimited` immediately, so the caller decides whether to wait ten minutes. |
| `5xx` (incl. `503`) | exponential backoff |
| `Transport` | exponential backoff |

**Backoff:** `delay(n) = min(base * 2^n, max_delay) * (0.5 + random()/2)` with
`base = 500 ms`, `max_delay = 30 s`, `n` zero-based. Full-width jitter in the lower
half so two clients started together diverge. The randomness source is the
language's default PRNG; it is **not** required to match across languages, and
conformance tests MUST NOT assert on delays.

**`Retry-After` as an HTTP-date.** RFC 9110 allows either a delay in seconds or an
HTTP-date, and a client **MUST** handle both:

1. An all-digits value is a delay in seconds. Use it.
2. Otherwise **parse it as an IMF-fixdate** (`Wed, 21 Sep 2026 02:36:00 GMT`) and
   convert to a delay: `seconds = max(0, date - now)`, where `now` comes from the
   client's **injectable clock** — the same one §6.6 uses for `.bak` timestamps —
   so tests are deterministic and never depend on wall-clock time.
3. A value that parses to a time **in the past** yields `0`, which means retry
   immediately (once, subject to `max_retries`), not "absent".
4. A value that parses as **neither** is treated as **absent**: fall back to the
   backoff below. Do not guess, and do not fail the request over a bad header.

The resulting delay then goes through the same `max_retry_after` cap as a numeric
one, so an HTTP-date an hour out raises `RateLimited` rather than blocking. The
computed seconds are what §3.3 exposes as `rate_limit.retry_after`, so a caller
never has to parse a date themselves. Clients **SHOULD** also keep the raw header
string available (`retry_after_raw`) for diagnostics.

`fixtures/http/error_429_retry_after_http_date.json` pins this. It carries a
`clock.now` field giving the exact instant the test must inject, and
`expect.retry_after_seconds` giving the answer.

🔴 **Never retry a 4xx other than 429.** A `400`, `401` or `404` is deterministic;
retrying it burns quota to receive the same answer.

**Idempotency:** every endpoint is a `GET`, so retry is always safe. If a client
later gains a non-GET method, retry MUST be re-evaluated, not inherited.

`Retry-After` and backoff sleeps count against nothing; `timeout` is **per attempt**,
not per call. A client SHOULD expose an optional overall `deadline`.

### 5.3 What is exposed on success

Every successful result carries: the decoded body, the envelope, `rate_limit`
(§3.3), `etag`, `from_cache`, and the HTTP `status` (200 or 304). Nothing about the
response is hidden from the caller.

---

## 6. GossipConfig — pure, offline, deterministic

Operates on `override_gossip_config.json`. **No network, no clock, no environment
lookups, no restart of anything.** Given the same input it produces the same bytes
on every platform, in every language, forever.

> 🔴 **LOUD DOC NOTE — required verbatim (modulo formatting) in every language's
> `GossipConfig` module docstring, in `save`'s docstring, and in the README:**
>
> The node reads `override_gossip_config.json` **when it starts**. Writing this
> file does **not** steer a running node: the change takes effect at the node's
> next start. This library will **never** restart, signal, or otherwise disturb
> your node — that is the operator's decision, at the operator's moment.
> Restarting a node *to apply a config* while it is behind can make a lagging box
> worse.

The node's file is `$HOME/override_gossip_config.json`.

### 6.1 Constructors and I/O

| method | behaviour |
|---|---|
| `parse(json_text)` | decode; **preserve unknown keys verbatim**; do not validate. Invalid JSON ⇒ `Decode`. A non-object top level ⇒ `Invalid{parameter: "$", reason: "expected a JSON object"}`. |
| `load(path)` | read + `parse`. Missing file ⇒ `NotFound{path}`; a client MUST NOT invent an empty config for a missing file, because an empty config is the crash-loop shape. `load_or_new(path, chain)` is the explicit opt-in to that. |
| `new(chain, root_ips)` | build from scratch. `root_ips` accepts bare IPv4 strings **or** `{"Ip": …}` entries on input and always stores the object shape. |
| `to_json()` | serialize per §6.3. Does **not** validate — `save` validates. `to_json_validated()` MAY be offered. |
| `save(path, opts)` | §6.5. |

**Placement of `load` and `save`.** These two are the only members of §6 that touch
a filesystem. A language whose core must stay filesystem-free — a browser bundle, a
`no_std`-ish or offline-only build — **MAY** expose them as **free functions in a
separate module** (TypeScript's `…/gossip-fs`, a Rust `fs` feature, an Elixir
`GossipConfig.FS`) rather than as methods on the config value. The **behaviour is
identical** and the conformance runner drives them the same way; only the call
syntax differs. Everything else in §6 is pure and stays in the core module.

**Default path.** Clients expose `default_config_path()` = `$HOME/override_gossip_config.json`.
It is a *default*, never a discovery: a client **MUST NOT** inspect a running
process, systemd unit, or `/proc` to guess the path. 🪤 Whether the node resolves the
file against `$HOME` or against its working directory is unverified (the two
coincide on a standard install). Each client's docstring says so, and emitting to stdout for the operator to place is presented as
the safest option.

### 6.2 Field algebra

Roots: `root_ips() -> [string]` (bare IPv4 strings, in file order — the `{"Ip": …}`
wrapper is an encoding detail and does not leak into this accessor),
`has_root_ip(ip)`, `add_root_ip(ip)` (appends; a duplicate is a **no-op**, not an
error), `remove_root_ip(ip)` (absent is a no-op), `set_root_ips(ips)` (replaces
wholesale, preserving given order, de-duplicating with first-wins).

Reserved peers: `reserved_peer_ips()`, `add_reserved_peer_ip(ip)`,
`remove_reserved_peer_ip(ip)`, `set_reserved_peer_ips(ips)`. 🪤 **Bare strings.**
Round-tripping one shape through the other silently produces a file the node will
not accept; both shapes MUST have a test.

Remaining contract fields: `set_chain(chain)`, `chain()`, `set_try_new_peers(bool)`,
`try_new_peers()`, `set_n_gossip_peers(int)`, `n_gossip_peers()`,
`set_split_client_blocks(bool)`, `split_client_blocks()`.

- `set_n_gossip_peers` **rejects** anything outside `[8, 100]` with
  `Invalid{parameter: "n_gossip_peers"}`. §10, D-C: we refuse, loudly, at both
  ends rather than clamp.
- `set_split_client_blocks(false)` **removes the key** (an absent key means `false`). `set_split_client_blocks(true)` writes `true`. An explicit `false`
  already present in a parsed file is preserved by round-trip — only an explicit
  setter call removes it. `unset_split_client_blocks()` removes it in either case.
- Unsetting in general: every `set_*` accepts the language's null to remove the key,
  except `set_root_ips` (see `validate`) and `set_chain`.

All mutators return the config (fluent) **and** mutate in place where that is the
language's idiom; either is acceptable as long as the `fixtures/gossip` runner can
apply an ordered list of operations to one config value.

### 6.3 Serialization — byte-exact across four languages

`to_json()` produces UTF-8 text, and all four implementations **MUST** produce the
identical byte string for the identical logical input. Rules:

1. **Key order is fixed**, for known keys, in exactly this order — the order of the
   live minimal file, extended by the upstream reference example:
   `chain`, `root_node_ips`, `try_new_peers`, `reserved_peer_ips`,
   `n_gossip_peers`, `split_client_blocks`.
   Absent keys are skipped, not emitted as null.
2. **Unknown keys are preserved** and emitted **after** all known keys, sorted
   lexicographically by Unicode code point. Their values are re-emitted with the
   same rules (nested objects: keys sorted; arrays: order preserved).
3. Indent is **2 spaces**, nesting-proportional. Separators are `","` between items
   (newline follows) and `": "` between key and value — one space after the colon,
   none before.
4. Arrays and objects are always expanded across lines when non-empty. An **empty
   array is `[]`** and an **empty object is `{}`**, on one line.
5. A single **trailing newline** (`\n`, never `\r\n`) terminates the file. Exactly
   one.
6. Non-ASCII characters are emitted **literally as UTF-8**, not `\u`-escaped.
   Escaping follows RFC 8259 minimal escaping (`"` `\` and C0 controls only;
   `/` is not escaped).
7. Integers are emitted without a decimal point or exponent. No float appears in
   this schema; a float found in an unknown key is re-emitted with the shortest
   round-trip representation, and a client MAY instead preserve the original token.

🪤 Rule 4 means a root entry renders **expanded**, across three lines:

```json
  "root_node_ips": [
    {
      "Ip": "192.0.2.10"
    }
  ],
```

not the one-line `{ "Ip": "192.0.2.10" }` the upstream contract's prose examples
show. Whitespace is not significant to the node's parser; what matters is that all
four languages agree, and a uniform rule is easier to agree on than an exception.

This is exactly the output of Python's
`json.dumps(obj, indent=2, ensure_ascii=False, separators=(",", ": ")) + "\n"` with
the key ordering of (1)+(2), which is the reference implementation the fixtures were
generated by.

### 6.4 `validate()`

Returns ok, or the **first** error in this order (deterministic, so all four
languages report the same one):

1. 🔴 `root_node_ips` absent, or present and **empty** ⇒
   `Invalid{parameter: "root_node_ips", reason: "refusing to emit an empty root_node_ips: hl-node asserts on a non-empty list at start and will crash-loop; there is no built-in fallback"}`.
   **This is the single most important guard in the library.** It fires in
   `validate`, in `save`, and in `merge_peers`/`from_peers`.
2. a `root_node_ips` entry that is not an object with the single key `Ip`
   (capital `I`) ⇒ `Invalid{parameter: "root_node_ips[<n>]"}`. `ip`, a bare string
   *inside a parsed file*, an extra `port` key: all rejected.
3. a root or reserved IP that is not a valid dotted-quad IPv4 ⇒
   `Invalid{parameter: …, reason: "<value> is not an IPv4 address"}`.
   **IPv6 is rejected** with
   `"IPv6 is out of contract: the peer API is IPv4-only and the node's support for an IPv6 literal here is unverified. Refusing rather than writing an untested value into a file whose failure mode is a crash loop."`
   (§10, D-B.)
4. a duplicate inside `root_node_ips`, or inside `reserved_peer_ips` ⇒
   `Invalid{parameter: …, reason: "duplicate <ip>"}`. The two lists are validated
   independently; an IP MAY appear in both.
5. `chain` present and not exactly `Mainnet` or `Testnet` ⇒ `Invalid{parameter: "chain"}`.
   Case matters. `chain` **absent** is allowed by `validate` but `save` warns (below).
6. `n_gossip_peers` outside `[8, 100]`, or not an integer ⇒ `Invalid`.
7. `try_new_peers` / `split_client_blocks` present and not boolean ⇒ `Invalid`.

**Warnings**, returned alongside ok and never fatal:

- `root_node_ips` length > **24**: *"a long root list makes the node slower to start
  and buys nothing; prefer a short list"*.
- length > **64**: the same, escalated.
- `chain` absent: *"emit `chain` matching the node's `--chain`; a mismatch stops the
  node from starting, and this library cannot tell which chain your node runs"*.
- `try_new_peers: false` with more than one root: *"whether `try_new_peers: false`
  restricts a multi-entry list to exactly that list is unverified; only a single
  root is known to pin"*.

**Warning codes.** A warning is `{code, message, detail?}`. The `code` is a stable
identifier the conformance suite asserts on; the `message` is prose and is not
asserted. The closed set:

| code | fires when |
|---|---|
| `many_roots_gt_24` | `root_node_ips` length > 24 |
| `many_roots_gt_64` | length > 64 — fires **instead of** `many_roots_gt_24`, never alongside it |
| `chain_absent` | `chain` key missing |
| `try_new_peers_false_multi_root` | `try_new_peers == false` and > 1 root |
| `truncated` | `merge_peers`/`from_peers` dropped candidates at `max_roots` |
| `exclude_overrides_keep_existing` | an IP was in both `exclude` and `keep_existing` |

🔴 **The codes are independent.** Each condition is evaluated on its own and every
one that holds fires. A config with 25 roots **and** `try_new_peers: false` reports
**both** `many_roots_gt_24` **and** `try_new_peers_false_multi_root` — a
`many_roots_*` warning does **not** suppress, absorb or outrank the pinning
warning. They describe two unrelated facts (this list is slow to boot; this list
may or may not pin), and swallowing one because the other fired would hide the
harder problem behind the easier one.

The **one** exception is the pair `many_roots_gt_24` / `many_roots_gt_64`, which
are **mutually exclusive**: above 64 the escalated code fires *instead of*, never
alongside, the gt-24 code. Nothing else in this table is exclusive with anything.

`fixtures/gossip/cases/validate_warns_many_roots_and_pinning_together.json` pins
this; an implementation that suppresses will fail it.

Clients expose warnings on the result of `validate`, `save` and
`update_gossip_config`; they are printed by the optional CLI and MUST NOT be
swallowed.

### 6.5 `merge_peers(peers, opts)` / `from_peers(peers, opts)`

`from_peers` builds a new config; `merge_peers` folds into an existing one. Both
take the **peer objects** from §3.2 (or bare IPv4 strings) and `opts`:

| opt | default | meaning |
|---|---|---|
| `max_roots` | **8** | hard cap on the resulting `root_node_ips` length |
| `mode` | `replace` | `replace` \| `append` \| `prepend` |
| `dedupe` | `true` | drop repeats, first occurrence wins |
| `keep_existing` | `[]` | IPs the operator pins; always retained and placed first |
| `exclude` | `[]` | IPs never to include, applied last and to everything |
| `chain` | inherited / required | on `from_peers`, required if not derivable |
| `try_new_peers` | unchanged / `true` on `from_peers` | |

**Why `max_roots` defaults to 8, not the upstream contract's ≤ 24 suggestion:** a
shorter root list starts the node faster, and a longer one buys nothing. 8 sits
inside the upstream's ≤ 24 guidance. A caller who wants the upstream default passes
`max_roots: 24`. Above 24 the validate warning fires; above 64 it escalates.
(§10, D-D.)

**Algorithm — deterministic, order-preserving, no sorting:**

1. `candidates` := the input peers' `id` (or the bare strings), **in the order
   given**. The client does **not** re-rank; the API already ranked them by `sort`.
2. Drop any candidate that is not a valid IPv4 ⇒ this is an `Invalid` error, not a
   silent drop.
3. `base` := for `mode = replace`, `[]`; for `append`, the existing roots; for
   `prepend`, `[]` with the existing roots appended after the candidates.
4. `merged` := `keep_existing` (in the order given, filtered to those that are valid
   IPv4) ++ (`base`/`candidates` per `mode`).
   - `replace`: `keep_existing ++ candidates`
   - `append`: `keep_existing ++ existing ++ candidates`
   - `prepend`: `keep_existing ++ candidates ++ existing`
5. Remove every member of `exclude`. 🪤 `exclude` beats `keep_existing`; an operator
   who asks for an IP to be excluded means it, and the conflict is surfaced as a
   **warning** naming the IP.
6. If `dedupe`, drop later repeats (first wins). If `dedupe` is false and duplicates
   remain, `validate` rejects the result — so `dedupe: false` is only usable on
   already-distinct input, and the error says so.
7. Truncate to `max_roots`, keeping the **head**. If truncation dropped anything,
   emit a warning naming the count.
8. 🔴 If the result is **empty**, raise the §6.4(1) error. **Never** write, never
   return a config with zero roots, never fall back to the previous roots silently.
   The message adds: *"the filter matched no peers — widen it, or pass
   keep_existing"*.
9. `try_new_peers`, `chain` and every other field are left exactly as they were
   (or set from `opts` on `from_peers`). Unknown keys survive untouched.

**Where merge-time warnings go.** `truncated` and `exclude_overrides_keep_existing`
arise during the merge, not from the resulting state, so a client that only
recomputed §6.4's state-derived warnings at save time would lose them. Normative:

- `merge_peers` / `from_peers` **return** their warnings from the call itself; and
- they **retain** them on the config value as **pending warnings**.
- `validate()`, `save()` and `update_gossip_config()` report the union of the
  pending warnings and §6.4's state-derived ones, **de-duplicated by code**. A
  warning raised during a merge therefore cannot be lost by a caller who ignored
  what the merge returned.
- Pending warnings live for the lifetime of the config value. The next
  `merge_peers`/`from_peers` **replaces** them with its own (it does not append),
  and `clear_warnings()` drops them. Nothing else clears them — in particular a
  successful `save` does not, so `save(); validate()` reports the same set twice
  rather than a shrinking one.
- Warnings are compared as a **set of codes**; order is not significant and a code
  never appears twice.

### 6.6 `save(path, opts)`

Order of operations, normative:

1. `validate()`. On error, **nothing is written** and the error propagates.
2. Render `to_json()` (§6.3).
3. If the target exists and its bytes are **identical** to the rendered text:
   **no-op** — do not write, do not create a `.bak`, do not touch mtime. Return
   `{written: false, changed: false, path, diff: ""}`.
4. If `opts.dry_run`: return `{written: false, changed: true, diff}` where `diff` is
   a unified diff (3 lines of context, `---`/`+++` headers naming the path, `\n`
   line endings) of the existing bytes against the rendered text. Nothing is
   written, and a missing target renders as a diff against an empty file.
5. If `opts.backup` (default **false**) and the target exists: copy it to
   `<path>.<UTC timestamp %Y%m%dT%H%M%SZ>.bak`, preserving mode. This is the only
   place a clock is read; it is injectable for tests.
6. Write to a temporary file **in the same directory** (`.<basename>.<rand>.tmp`),
   `fsync` the file, close, then **atomically rename** over the target. `fsync` the
   containing directory afterwards where the platform supports it. A temp file in
   `/tmp` is **wrong**: rename across filesystems is not atomic.
7. **Preserve the target's file mode** when it existed. For a new file, `0600`
   (this file names infrastructure the operator peers with; default-private is the
   right posture) unless `opts.file_mode` says otherwise. Never widen an existing
   mode.

🔴 **`file_mode`, not `mode`.** The permission-bits option is named **`file_mode`**
(`fileMode` in TypeScript) in `save`, in `update_gossip_config`, and in the CLI
(`--file-mode`). The name `mode` belongs to the **merge strategy** of §6.5
(`replace` | `append` | `prepend`) and to nothing else. The two would otherwise
collide the moment §7 takes a single options bag, and a caller who passed `0600`
expecting permissions would silently get an invalid merge strategy — or worse, a
caller who passed `"append"` expecting a merge would have it read as permissions.
Elixir and TypeScript already do this; **Python and Rust must follow.**
8. On any failure after step 6 begins, remove the temp file; the original is
   untouched by construction.

🔴 **Symlinks — follow the link, replace the target.** Step 0 of `save`, before
anything else touches the filesystem: if `path` is a symbolic link, resolve it
(following a chain, to a bound of **32** links) and use the **resolved** path as
the write target for steps 3–8 — the existence check, the mode to preserve, the
directory the temp file is created in, the rename, and the `.bak`. A resolution
that exceeds the bound, or that cannot be resolved because an intermediate link
is broken and its parent directory does not exist, is `Invalid{parameter: "path"}`.

The alternative — renaming over the link, which is what a naive atomic write
does — **replaces the operator's symlink with a regular file**. An operator who
pointed `$HOME/override_gossip_config.json` at a config-managed location did so
deliberately; silently turning it into an unmanaged regular file makes the node
and the operator's tooling read two different files from that moment on, and
nothing announces it. Following the link keeps their arrangement intact and keeps
the rename on one filesystem, which is what makes it atomic.

`result.path` is still the path the **caller passed**, and the diff headers name
that path, so a caller sees the name they used rather than a resolution they did
not ask about. A client **SHOULD** expose the resolved target as
`resolved_path` when it differs.

A dangling symlink whose parent directory exists is written through: the target
is created, the link is left alone. `save` never creates or removes a symlink.

`save` returns `{written, changed, path, diff, backup_path?, warnings}`. Calling
`save` twice with no intervening change **MUST** produce `written: false` the second
time (idempotence; a fixture case pins this).

🔴 `save` never restarts, signals, or reloads anything. See the loud note in §6.

---

## 7. `update_gossip_config(client, query, path?, opts)`

The one helper that joins the two namespaces. Exactly one round of work:

1. Collect peers: `iter_peers(query, max_items: opts.max_roots * opts.overfetch)`
   with `overfetch` defaulting to **4**, so that `exclude`/`keep_existing`/validity
   filtering still leaves enough candidates. A caller MAY pass an explicit peer list
   instead of a query and skip the network entirely.
2. 🔴 If the response carries `available: false`, raise `Unavailable{}` **before**
   touching any file. An unbuildable graph is not an empty peer set, and turning it
   into one would empty an operator's root list — the crash-loop shape.
3. `config` := `path` given and the file exists ⇒ `load(path)`; `path` given and
   absent ⇒ `new(chain, [])` (then step 4 must produce roots or it raises);
   no `path` ⇒ `new(chain, [])`.
4. `merge_peers(peers, opts)` (§6.5).
5. `validate()`; collect warnings.
6. If `path` and not `opts.no_write`: `save(path, opts)`.

Returns `{config, json, changed, diff, written_path?, warnings, envelope}`.
`json` is always present even when nothing was written, so "generate to stdout and
let the operator place it" is a first-class path. `changed` is false when the
rendered bytes equal what was already on disk.

The `chain` for a fresh config comes from `opts.chain`, else the response
envelope's `chain`, else it raises `Invalid{parameter: "chain"}`. A client
**MUST NOT** default it to `Mainnet`: a `chain` that disagrees with the node's
`--chain` stops the node from starting, and guessing is how that happens.

---

## 8. The optional CLI

🔴 **A CLI is genuinely optional and a client may ship none.** This section is not a
requirement to build one — it is the shape a CLI must take *if* it exists, so that
a runbook written against one language works against any other that also ships
one. A language without a CLI is conformant. The root README's feature matrix
records which languages actually ship one; do not infer from this section that all
four do.

If built: binary/task name `hl-peers` (`mix hl_peers` in Elixir), flags exactly as
below, and `--file-mode` rather than `--mode` for permission bits (§6.6).

```
hl-peers query [--chain Mainnet|Testnet] [--network mainnet|testnet]
               [--cc DE,NL] [--region …] [--asn …] [--tier 1] [--max-tier 2]
               [--role …] [--label sentry] [--connectable yes] [--accepting]
               [--state full] [--max-blocks-behind 5] [--min-uptime 0.95]
               [--max-latency-ms 80 --vantage eu] [--seen-within 1h]
               [--version …] [--sort tier|blocks_behind|out_deg|last_seen|random]
               [--limit 50] [--all] [--max-items 10000] [--fields id,tier,cc]
               [--country JP] [--serves-mempool true,null]
               [--auction holds_slot,bidding]
               [--format json|gossip-config] [--output json|table|ips]

hl-peers facets [--cc …] [--tier …] [--role …] [--chain …] [--network …]

hl-peers peer <ip> [--network mainnet|testnet]

hl-peers gossip show   --path ~/override_gossip_config.json
hl-peers gossip validate --path …
hl-peers gossip update --path … [query flags] [--max-roots 8]
                       [--mode replace|append|prepend] [--no-dedupe]
                       [--keep 192.0.2.5,…] [--exclude 198.51.100.9,…]
                       [--chain Mainnet] [--try-new-peers|--no-try-new-peers]
                       [--n-gossip-peers 20] [--file-mode 0600]
                       [--backup] [--dry-run] [--stdout]

Global: --base-url URL  --api-key KEY  --timeout SECS  --max-retries N
        --no-cache  --quiet  --verbose  --json-errors
```

- `--chain` is accepted by **all three** of `query`, `facets` and `gossip update`.
  Without it on `query` there is no way to reach `ChainNotServed` from the CLI,
  and a `Testnet` operator has no way to confirm from the command line that this
  deployment will not answer for their chain.
- `--network` is accepted by `query`, `facets`, `peer` and `gossip update`, and by
  any network-map command a CLI ships. Unset sends nothing (mainnet).
- `--dry-run` prints the unified diff to stdout and exits **0** if there is no
  change, **0** with the diff if there is one; it never writes.
- `--stdout` prints the rendered JSON instead of writing, so
  `hl-peers gossip update --stdout > override_gossip_config.json` works.
- Exit codes: `0` ok · `1` a client/validation error (`Invalid`, `BadRequest`,
  `FilterUnavailable`) · `2` usage · `3` `RateLimited` · `4` `Unauthorized` ·
  `5` `NotFound` · `6` `Unavailable`/`Server`/`Transport`.
- Warnings go to **stderr**, always, even under `--quiet`… except `--quiet` may
  suppress the informational ones; the `>24 roots`, `>64 roots`, `truncated` and
  `exclude beat keep_existing` warnings are **never** suppressed.
- 🔴 The CLI **never** restarts a node and has no `--restart` / `--apply` flag.
  After a successful write it prints, to stderr:
  `wrote <path>. The node reads this file when it starts — the change takes effect on the node's next start, which is yours to schedule.`

---

## 9. Testing obligations

Every language directory **MUST** ship:

1. **A generic gossip case runner** over `fixtures/gossip/cases/*.json`, driven by
   the format in `fixtures/README.md`. Not one test per case — one runner. All
   cases must pass, including the byte-exactness ones.
2. **HTTP fixture tests** against `fixtures/http/*.json`, either by stubbing the
   transport or by running `tools/mock_server.py`. Must cover: each endpoint, each
   of the four preset queries, the 3-page pagination walk, `304`, `429`
   (with header parsing and the retry decision), `400 filter-unavailable`, `401`,
   `404`, `500`.
3. **A retry test** asserting on the *decisions* (which statuses retry, how many
   attempts, that `Retry-After` was read) and **never** on wall-clock delays.
4. **A round-trip test** proving unknown keys survive and that the live minimal form
   re-renders byte-identically.
5. **Signals, network map and gossip helper:** the mempool/auction fixtures (`peers_owner_q1_mempool_cc_de`,
   `peers_serves_mempool_unknown`, `peers_auction_holds_slot_or_bidding`,
   `error_400_filter_removed`, `error_400_filter_unavailable_mempool`) decoded into
   the typed `mempool` / `auction` objects; both `/network-map/*` fixtures; the
   `API-Version` header exposed; retries off by default (one 503 ⇒ one request);
   `RateLimited.reset_at` from the injected clock; and the §4.7 **golden
   gossip-config test** (`to_json()` and the saved bytes).
6. **States, cities and the flood 429:** `peers_state_listening` (the `listening` state decoded and
   selectable), `Peer.city` on every fixture row, the graph's `cities` /
   `attribution` and the summary's `serves_mempool` / `auction` / `cities` /
   `attribution`, `network_map_graph_mempool_auction` (the two map filters on
   the wire, and their client-side `Invalid`), and `error_429_ip_flood`.
7. **Testnet (beta):** `peers_testnet`, `peer_found_testnet`, `facets_testnet`,
   `network_map_graph_testnet`, `network_map_summary_testnet` (the request carries
   `network=testnet`; `network`, `chain: Testnet`, each row's `version` and geo/ASN
   decoded), `error_400_network_not_served` (a `BadRequest` with
   `parameter == "network"`), the client-side `Invalid{parameter: "network"}` for an
   out-of-enum value, the client-level `network` default and its per-call override,
   and a **mainnet golden**: with `network` unset the request URL is exactly what it
   was before (no `network` key), on every route.
8. **No network access in the default test run.** `HL_PEER_API_BASE_URL` pointing at
   a real host must not be required, reachable, or consulted.

---

## 10. Resolved ambiguities and departures

Recorded here so all four clients resolve them identically. Each is a **client-side**
decision; none is a change to the upstream spec.

- **D-A — free-tier rate numbers conflict.** The OpenAPI `429` example `detail`
  says a free key gives *"120/minute and 50000/day"*; the published tier table
  says free is **10 rps / 100 000 per month**.
  **Resolution:** clients hardcode **neither**. All limit knowledge comes from the
  `RateLimit-*` headers at runtime; the `detail` string is surfaced verbatim to the
  caller and never parsed. `README.md`'s tier table quotes the published numbers and
  notes they may be tuned.
- **D-B — IPv6.** The OpenAPI says IPv4-only for `/peers/{ip}` and every `format:
  ipv4`. The gossip contract does not mention IPv6 at all. **Resolution:** reject
  IPv6 everywhere with the explicit message in §6.4(3), which says the node's
  behaviour is unverified. Rejecting is the safe side of an unknown whose failure mode
  is a crash loop.
- **D-C — `n_gossip_peers`: clamp or refuse?** The accepted range is `[8, 100]`.
  **Resolution:** the client **refuses** out-of-range
  values at both ends. Silently clamping would write a number the operator did not
  choose into a file they will read back later.
- **D-D — `max_roots` default 8 vs the contract's ≤ 24.** **Resolution:** 8, for
  the start-up reason in §6.5, which is inside the contract's guidance
  rather than against it. 24 remains the documented "upstream default" value and
  the warning threshold.
- **D-E — the GossipConfig shape differs between the two upstream documents.** The
  OpenAPI `GossipConfig` schema has three properties and requires all three; the
  gossip contract has **six** fields and states that all keys except `root_node_ips`
  are optional in practice, with the live file carrying three. **Resolution:**
  `spec/gossip-config.schema.json` follows the **contract** — six known fields,
  `root_node_ips` the only required one, `additionalProperties: true`. The OpenAPI
  schema is treated as a narrower description of what `format=gossip_config`
  happens to emit, not as the file's schema.
- **D-F — key order.** Neither document specifies one, but byte-identical output
  across four languages requires it. **Resolution:** the fixed order in §6.3(1),
  which is the live file's order extended by the reference example's.
- **D-G — `available: false` in a config-building path.** The spec is explicit that
  this is not an error for a *query*. It is silent on what a generator should do.
  **Resolution:** it is an error (`Unavailable`) for `from_peers`/`update_gossip_config`
  specifically, because the alternative is emitting the crash-loop shape.
- **D-H — `cursor-expired` handling.** The spec says the server refuses a mixed
  page; it does not say what a paginating client should do. **Resolution:** surface
  it, never auto-restart (§4.2).
- **D-I — `Retry-After` larger than the cap.** Unspecified. **Resolution:** raise
  rather than sleep (§5.2), because a client library should not silently block a
  caller's thread for minutes.
- **D-J — `sort` default.** **Resolution:** clients send **no** `sort` unless the
  caller sets one, so the server's default governs and clients do not need a
  release if it changes.
- **D-K — problem+json vs the legacy `{"error": …}` body.** The peer paths use
  problem+json. **Resolution:** clients
  parse problem+json and fall back to status-only mapping for any other body shape,
  so a legacy-shaped error still produces the right variant.
- **D-O — `mempool` / `auction`.** The public shape is the pair of typed objects of §3.2 and the
  closed-enum filters `serves_mempool` / `auction` of §2.1; `stream_quality` stays
  unpublished, and the old `mempool=` filter answers `400 filter-removed`
  (`BadRequest`). The preset is `mempool_servers()`.
- **D-Q — retries off by default.** §5.2's policy applies once enabled.
- **D-R — `list_peers` refuses `format=gossip_config`**; the typed helper of
  §4.7 is the way in, and is required in all four clients.
- **D-S — the summary carries `cities` / `attribution` the upstream schema omits.** The live `/network-map/summary` returns the same `cities` and
  `attribution` keys as the graph; upstream's `NetworkMapSummary` schema does not
  list them. **Resolution:** clients decode them on the summary exactly as on the
  graph (optional, absent ⇒ null). `tools/gen_spec_fields.py` adds them to the
  summary field set with a note.
- **D-T — the producer's own nodes are ordinary rows.** The public API
  publishes what a scan of the public gossip network returns, the producer's own
  relays and nodes included, as other peers list them (`operator_nodes.count:
  null`). **Resolution:** clients add no filtering of their own,
  never special-case an IP, and treat `operator_nodes.count` as an informational
  nullable integer (a non-null count means the producer is running a count-only
  projection instead).
- **D-U — `network` (testnet, beta).** The API added an optional `network`
  parameter (`mainnet` default | `testnet`) on all five routes.
  **Resolution:** a closed enum, lower-case, case-sensitive client-side (the
  server lower-cases, but a client that accepts `Testnet` here would blur it with
  `chain`, which is capitalised). Unset ⇒ omitted, so a mainnet request is
  byte-identical to a 1.1 request and hits the same cache key. It is a builder
  field on `/peers` and `/peers/facets`, a per-call option on `get_peer` and the
  network map, and a client-level default (§1); a per-call value wins. `chain` and
  `network` are different questions: `network` picks which graph is read,
  `chain` filters/asserts what the server states; a testnet body states
  `chain: Testnet`. `network-not-served` (testnet switched off on the deployment)
  is a `BadRequest` with `parameter: "network"`, **not** a new error variant, so
  existing exhaustive matches keep compiling; callers test
  `parameter == "network"`. Testnet is **beta**: a small graph, no session data,
  approximate `blocks_behind`, frequent resets (`available: false` for a while is
  normal); clients do nothing special about any of it.
- **D-N — the route prefix.** The API uses plain paths at the host root of
  `api.hypervisor.gg`. **Resolution:** `base_path`
  default `""`, one constant per client; the option remains for proxies.
