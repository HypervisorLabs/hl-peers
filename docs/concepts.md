# Concepts

What the Peer API's values mean, and the handful of places where the obvious
reading is wrong. Everything here is a restatement of the upstream contract; the
normative source is `../spec/peer-api-v1.openapi.yaml` and `../CLIENT_SPEC.md`.

---

## The shape of an answer

Every successful body carries an envelope: `version`, `available`, `stale`,
`chain`, `generated_at`, `next_sweep_at` (when the data is next refreshed).

🔴 **`available: false` is not an error and not an empty result.** It means the
producer could not build a graph at all. The `peers` list that comes with it is
empty, and that emptiness **is not a finding about the network**. Clients surface
the flag and never convert it into an exception — with one exception, in the
config-generating path, where an empty peer list would otherwise become an empty
root list, which crash-loops a node.

Check `available` **before** `stale`: a dark subsystem reports both as false.

`503` is reserved for the build path actually raising. A graph that merely cannot
be built right now is a `200`.

---

## Filters

Filter families **AND** together; CSV values within one family **OR**. So
`?cc=DE,NL&tier=1` is *(DE or NL) and tier 1*.

An unrecognised member of a **closed** enum is a `400`, never a silent drop — a
typo'd bucket that quietly matched nothing would read to an operator as a finding
about the network. `limit` and `cursor` are clamped rather than rejected.

### 🔴 `filter-unavailable`: the rule that makes this API trustworthy

A filter on an attribute the producer **does not collect** answers
`400 filter-unavailable` — never an empty list. `?asn=AS24940` returning `[]` would
tell you "there are no peers in that AS", which is false; the truth is "we do not
know". Today `region`, `asn`, `min_uptime`, `max_latency_ms` and `version` are all
in this state.

Call `facets()` first. A facet with `available: false` is an attribute that is not
collected; a facet with `available: true` and an empty `values` list is an
attribute that *is* collected and currently matches nothing. **Two different
answers**, and clients keep them distinguishable.

### Tier

`tier` is the **distance from the network's sentry nodes** in the peer graph,
recomputed at every data refresh.

- `tier: 0` means *this node is a sentry*. It is **not** a hop count to a
  validator. A third-party explorer's "hops to validator" is a different quantity
  and the two will legitimately differ.
- `tier: null` means **the node was not placed**. It is **not** tier 0 and
  clients never synthesise a value for it.
- `tier=1` is an **exact** match. `max_tier=1` is a **ceiling** and also returns
  tier 0. They are different questions; both exist; neither is sugar for the other.

`sentries()` uses `label=sentry` rather than `tier=0`. They are different
questions and may diverge.

### The three-state enum, which is never folded

`connectable` ∈ `"yes"` | `"no"` | `"unknown"` *(strings, not booleans)*

`unknown` means **we never observed this**. It is not `no`, not `false`. No client offers an accessor that collapses `unknown` into a boolean
false; where a boolean convenience exists it is named for the positive
confirmation only and documented as excluding `unknown`.

### Mempool and the gossip auction — also never folded

Since the 10-01 API every peer carries two
objects:

- `mempool` = `{serves, last_checked, verdict, first_frame_ms, source}` — does
  the peer serve the mempool stream? `source` (`probe` | `relay`) says which of the
  producer's measurements produced the verdict; `verdict` is an open machine token
  (e.g. `serves`); `first_frame_ms` is the time to the first mempool message.
  **`serves: null` means unknown — it is not "no".**
- `auction` = `{status, slot, since, last_bid_at}` — its standing in the on-chain
  gossip read-priority auction: `holds_slot` (with the lowest `slot` held and
  `since`), `bidding` (bid within the last 6 hours), `bid_before`, or `null` (never seen).

Filters: `serves_mempool=true|false|null` and
`auction=holds_slot|bidding|bid_before|null`, CSV, closed enums. `stream_quality`
is still not published, and the old `mempool=` filter answers `400 filter-removed`.

### City clusters, not coordinates

The network map endpoints (`/network-map/graph`, `/network-map/summary`) describe
the same graph you can see drawn live at
[hypervisor.gg/network](https://hypervisor.gg/network).

Where the producer serves city positions, `/network-map/graph` and `/summary`
carry `cities` (`{id, name, cc, lat, lon, peers, tiers}`; `lat`/`lon` are the
city's own point rounded to 0.1°) and an `attribution` string to show wherever the
map is shown. Each peer then carries `city`, the id of its cluster, or `null` when
it is placed by its cloud `region`, its country, or not at all. No per-peer
coordinate is ever published. When the producer does not serve city positions,
`cities` and `attribution` are **absent** (the clients keep that distinct from an
empty list) and every `city` is `null`.

### 🪤 "Not full" is an estimate, not a guarantee

`accepting=true` is exactly `connectable=yes`: an **estimate** of whether the peer
has room for another inbound peer. It can be wrong in both directions, and
`unknown` is not `no`.

"Not full" is how operators say it, which is why the filter is named `accepting`
and why the caveat is attached to one schema rather than scattered.

### 🪤 `state` — `responsive` is the common case

`state` ∈ `responsive` | `open` | `full` | `syncing` | `listening` |
`unreachable` | `unknown`. `responsive` = the peer answered our scan; `open` =
accepting new inbound peers; `full` = not accepting new inbound peers right now;
`syncing` = catching up; `listening` = accepts inbound connections, other
details unknown (usually `tier: null`); `unreachable` = not reachable; `unknown` =
never observed. For the estimated "has a free inbound slot" use `accepting`. It is an **open** enum:
an unrecognised value renders plain, never an error.

`role` and `labels[]` are open sets too, and `role` is sourced from third-party
enrichment — "a hint, never truth". Prefer `label` for sentry selection.

### Always `null`

`version` (not published). `null` is "not provided", never zero, never a value.

### Geography

`cc` (ISO-3166-1 alpha-2; `country` is an alias) covers every published peer, with
`cc_confidence` (`high` | `medium` | `low`: how confident the label is) and
`cc_source` (`cloud-range` | `db` | `db+rtt-confirmed`, taken as values); a label we
cannot stand behind is published as `cc: null`. `asn` / `org` come from the same
database. `region` (`provider:region`) is the cloud provider's region, published
**only** for cloud-hosted peers. While a deployment has no
geo data loaded, geo filters answer `400 filter-unavailable`. Every response
carries the database `attribution`. The presets take a tagged geo argument so you
cannot pass one where you meant the other by accident.

### Sorting

`sort` ∈ `last_seen` (default, newest first) | `tier` | `blocks_behind` | `out_deg` | `random`.
An unknown value is a `400`.

`random` exists on purpose: if every client sorts by the same key, every client
picks the same head peers, and a peer-selection API becomes a centralising force on
the network it describes. It is seeded per key per data refresh so your pages stay stable
within a generation. Clients send **no** `sort` unless you set one, so the server's
default governs.

### Pagination

Opaque cursors from `next_cursor`. 🪤 A cursor is bound to the graph generation it
was issued in; paging across a data refresh answers `400 cursor-expired` rather
than a silently-mixed page. Clients surface that error and **never** auto-restart
the walk, because restarting would produce exactly the mixed result the server
refused to serve. Catch it and start over if that is what you want.

`iter_peers` carries a `max_items` guard defaulting to 10 000. The full directory
is several hundred nodes — a handful of pages at the anonymous ceiling of 50.

---

## Rate limits

Three tiers; the table and its caveats are in the root [README](../README.md).
What matters for a client author:

- **Anonymous is a first-class tier.** Omitting a key is never a `401`. A `401`
  means a key was supplied and is invalid.
- **Every response carries the limit state** — `RateLimit-Limit`,
  `-Remaining`, `-Reset` (seconds from now, not an epoch), `-Policy`, and
  `Retry-After` on a `429`. Clients parse these off 200s, 304s and errors alike and
  expose them on both results and errors. A missing header is `null`, never 0.
- **Never parse the `detail` prose for numbers.** The upstream documents disagree
  with each other about the free tier's limits; the headers are the truth.
- **The `429` body carries a `signup_url`**, and the error type carries it through
  so a CLI can print the way out.
- **The `429` error carries `reset_at`**, an absolute UTC time to wait until.
- **Retry policy: off by default.** With `max_retries > 0`: honour
  `Retry-After` on a `429`, capped (default 60 s — above the cap the client raises
  rather than blocking your thread for minutes); exponential backoff with jitter
  on `5xx` and transport faults; **never** retry a `4xx` other than `429`.
- **`API-Version: 1`** is on every response; clients expose it as `api_version`.

---

## Caching, and why polling is pointless

The data refreshes on its own schedule. Polling faster than once a minute returns
identical bytes and spends quota.

- `Cache-Control: public, max-age=60, stale-while-revalidate=240`.
- `ETag` is a strong validator over the **response body only**, so the same filters
  against the same build produce the same ETag across replicas.
- A `304` from `If-None-Match` **costs no quota unit**. Conditional polling is the
  behaviour the API wants to subsidise, so every client ships a small in-memory
  ETag cache, on by default, and returns the stored body with `from_cache = true`.
- The cache always revalidates; it does not honour `max-age` as a hard TTL. It
  exists to make polling free, not to serve you stale bytes.
- `next_sweep_at` is the data's own clock (when the data is next refreshed). The polling helper sleeps until then
  plus a small per-instance jitter, and refuses an interval below 60 s.

---

## Privacy posture, briefly

The published graph is what a scan of the public gossip network returns. The
producer's internal labels and measurement detail never cross the boundary.
`in_deg`, `out_deg` and `feeders[]` are counted over the *published* edge set.

These are IP addresses of internet infrastructure — but an operator may be a
natural person. Treat the directory accordingly: it is for picking peers, not for building a
dossier on an operator.
