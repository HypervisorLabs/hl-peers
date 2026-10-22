# `hl-peers` (Rust)

A client for the Hyperliquid **Peer API**, and an offline editor for hl-node's
`override_gossip_config.json`. Implements [`CLIENT_SPEC.md`](../CLIENT_SPEC.md).

**Version 1.1.0**, tracking the live API at **`https://api.hypervisor.gg`**
(plain paths at the host root: `/peers`, `/peers/{ip}`, `/peers/facets`,
`/network-map/graph`, `/network-map/summary`). 

## Install

From git or by path:

```toml
[dependencies]
hl-peers = { git = "https://github.com/HypervisorLabs/hl-peers" }   # the crate lives in rust/
# or
hl-peers = { path = "../hl-peers/rust" }
# offline config editing only — no HTTP dependency at all:
hl-peers = { path = "…", default-features = false }
# + the sync facade / the hl-peers binary:
hl-peers = { path = "…", features = ["blocking"] }   # or ["cli"]
```

A crates.io release of `hl-peers` will follow.

---

## 🔴 Safety notes

**The node reads `override_gossip_config.json` when it starts.**
Writing this file does **not** steer a running node: the change takes effect at the node's next start. This library will **never** restart, signal, or otherwise
disturb your node — that is the operator's decision, at the operator's moment.
Restarting a node *to apply a config* while it is behind can make a lagging box worse.

The node's file is `$HOME/override_gossip_config.json`.

And the guard that matters most: **this library refuses to emit an empty
`root_node_ips`.** hl-node asserts on a non-empty list at start and will
crash-loop; there is no built-in fallback. A filter that matched nothing raises
rather than writing.

---

## Quickstart — async

```rust,no_run
use hl_peers::{Client, Geo, PeerQuery};

# async fn run() -> hl_peers::Result<()> {
let client = Client::builder()
    // .base_url("https://api.hypervisor.gg")   // the default; or $HL_PEER_API_BASE_URL
    .api_key("hlp_live_…")                      // optional: anonymous is a supported tier
    // .max_retries(3)                          // opt in: auto-retry is OFF by default
    .build()?;

let page = client
    .list_peers(&PeerQuery::not_full(Geo::cc(["DE"])))
    .await?;

// Check `available` BEFORE `stale`: a dark subsystem reports both as false.
if !page.envelope().available {
    eprintln!("the producer could not build a graph — this is NOT 'no peers matched'");
    return Ok(());
}
for p in &page.peers {
    println!("{} tier={:?} connectable={:?}", p.id, p.tier, p.connectable);
}
println!("{:?} requests left this window", page.rate_limit.remaining);
println!("API-Version {:?}", page.api_version);   // "1"
# Ok(()) }
```

## Quickstart — blocking

Enable the `blocking` feature.

```rust,no_run
use hl_peers::{BlockingClient, PeerQuery};

# fn run() -> hl_peers::Result<()> {
let client = BlockingClient::new()?;   // https://api.hypervisor.gg
let page = client.list_peers(&PeerQuery::tier1())?;
println!("{} peers", page.peers.len());
# Ok(()) }
```

---

## The owner's example queries

| question | code | wire |
|---|---|---|
| serves mempool, in DE | `PeerQuery::mempool_servers().cc(["DE"])` | `cc=DE&serves_mempool=true` |
| not full, in SG | `PeerQuery::not_full(Geo::cc(["SG"]))` | `accepting=true&cc=SG` |
| tier-1 peers | `PeerQuery::tier1()` | `tier=1` |
| every sentry | `PeerQuery::sentries()` | `label=sentry` |

## Every filter

`PeerQuery` is immutable and chainable; list filters take a list (never a
hand-built CSV) and are joined with an encoded `%2C`.

| setter | wire | notes |
|---|---|---|
| `.chain(Chain::Mainnet)` | `chain` | closed enum |
| `.cc(["DE","NL"])` / `.country(["JP"])` | `cc` / `country` | upper-cased; `country` is a server-side alias (both ⇒ union) |
| `.region(["aws:eu-central-1"])` | `region` | 🪤 cloud-hosted peers only |
| `.asn([…])`, `.role([…])`, `.label([…])`, `.version([…])`, `.fields([…])` | CSV | open sets — never validated locally |
| `.tier([TierFilter::Exact(1), TierFilter::Unplaced])` | `tier=1,null` | **exact**; `null` = never placed |
| `.max_tier(2)` | `max_tier` | a **ceiling** |
| `.connectable([Connectable::Yes])` | `connectable` | closed: `yes`/`no`/`unknown` |
| `.accepting(true)` | `accepting` | exactly `connectable=yes` |
| `.state([PeerState::Responsive, PeerState::Open])` | `state` | closed: `responsive`/`open`/`full`/`syncing`/`listening`/`unreachable`/`unknown` |
| `.serves_mempool([Some(true), None])` | `serves_mempool=true,null` | closed; `None` = `null` = **unknown**, not `false` |
| `.auction([Some(AuctionStatus::HoldsSlot), None])` | `auction=holds_slot,null` | closed; `None` = never seen in the auction |
| `.try_new_peers(false)` | `try_new_peers` | only meaningful with `format=gossip_config` |
| `.max_blocks_behind(5)`, `.min_uptime(0.95)`, `.max_latency_ms(80).vantage("eu")`, `.seen_within("1h")` | as-is | validated per §2.2 |
| `.sort(Sort::Tier)`, `.limit(50)`, `.cursor(…)`, `.format(Format::Json)` | as-is | no `sort` is sent unless you set one |

```rust
use hl_peers::{AuctionStatus, PeerQuery};

// Owner query 1 whole: mempool servers in Germany.
let q = PeerQuery::mempool_servers().cc(["DE"]);
assert_eq!(q.to_query_string().unwrap(), "cc=DE&serves_mempool=true");

// Peers holding a read-priority slot, or bidding for one.
let q = PeerQuery::new().auction([Some(AuctionStatus::HoldsSlot), Some(AuctionStatus::Bidding)]);
assert_eq!(q.to_query_string().unwrap(), "auction=holds_slot%2Cbidding");

// "unknown" is not "does not serve": ask for the unknowns explicitly.
let q = PeerQuery::new().serves_mempool([None]);
assert_eq!(q.to_query_string().unwrap(), "serves_mempool=null");
```

Every peer carries two typed objects, `mempool` and `auction`; an all-`None`
value means **unknown**, and a peer from an older server that omits them decodes
to that, never to an error:

```rust
# use hl_peers::{AuctionStatus, Peer};
let p: Peer = serde_json::from_str(r#"{"id":"192.0.2.10",
    "mempool":{"serves":true,"last_checked":"2026-09-21T02:30:00Z","verdict":"serves","first_frame_ms":412,"source":"probe"},
    "auction":{"status":"holds_slot","slot":3,"since":"2026-09-20T11:00:00Z","last_bid_at":null}}"#).unwrap();
assert_eq!(p.mempool.serves, Some(true));
assert_eq!(p.auction.status, Some(AuctionStatus::HoldsSlot));

let old: Peer = serde_json::from_str(r#"{"id":"192.0.2.11"}"#).unwrap();
assert_eq!(old.mempool.serves, None);   // unknown — NOT false
```

`Peer` also carries The geo fields `cc`, `cc_confidence`
(`high`/`medium`/`low`), `cc_source` (`cloud-range`/`db`/`db+rtt-confirmed`),
`asn`, `org`, plus `uptime` and `feeders`; the envelope carries `attribution`
(the geo-data notice to display alongside them).

Traps worth repeating:

- `tier` is **exact**; `max_tier` is a **ceiling**. `max_tier=1` also returns
  tier 0. This crate will not map one to the other.
- `sentries()` uses `label=sentry`, **not** `tier=0`. They are different questions and may give different answers.
- `not_full` is an **estimate** of whether the peer has room for another inbound
  peer, **not a guarantee**; it can be wrong in both directions.
- The geo argument is **tagged** (`Geo::cc` / `Geo::region`), never a bare
  string. 🪤 `region` is published **only for cloud-hosted peers**
  (`provider:region`, for cloud-hosted peers) and answers
  `400 filter-unavailable` while no geo data is loaded; `cc` is the broad geo
  key. That 400 arrives as `Error::FilterUnavailable` and is never swallowed
  into an empty list.
- `mempool_servers()` excludes peers with `serves: null` — **unknown**.
  "unknown" is not "does not serve". The old `mempool=` filter answers
  `400 filter-removed` (`Error::BadRequest`).
- `state`, `connectable`, `serves_mempool` and `auction` are **closed** as
  filters: a member outside the set is refused locally with `Error::Invalid`,
  and case matters. (As *decoded* peer fields `state` and `auction.status` stay
  open, so a new server value is `Other(…)`, never an error.)
- `ClientBuilder::base_path` (default `DEFAULT_BASE_PATH` = `""`, plain paths
  at the host root) exists for a proxy that mounts the API under a prefix.
- No `sort` is sent unless you set one, so the server's default governs (D-J).
- Query parameters are emitted in lexicographic order and percent-encoded,
  commas included, so two clients produce the same URL and the same cache key.

Pagination, with a hard guard that defaults to 10 000 items:

```rust,no_run
use hl_peers::{Client, IterOpts, OnTruncate, PeerQuery};
# async fn run(client: Client) -> hl_peers::Result<()> {
let mut s = client.iter_peers(&PeerQuery::tier1(), IterOpts::default().with_max_items(500));
while let Some(peer) = s.next().await {
    println!("{}", peer?.id);
}
if s.truncated() { eprintln!("stopped at the guard"); }
# Ok(()) }
```

`PeerStream` also implements `futures_core::Stream`, and `collect_all()` /
`Client::list_all_peers` are the eager forms. The whole directory is ~900 nodes,
which is 18 anonymous pages at `limit=50`.

🪤 A cursor is bound to the graph generation it was issued in. Paging across a data refresh answers `400 cursor-expired`, which surfaces as
`Error::CursorExpired` and is **never** auto-restarted — that would mix two
generations into one result set, which is exactly what the server refused to do.
Catch it and start the walk over.

---

## Gossip config recipes

### Let the server render it: `list_peers_as_gossip_config`

`format=gossip_config` asks the server for a ready `override_gossip_config.json`.
`list_peers` refuses that format (its body has no `peers` array); this is the
typed way in. It returns a `GossipConfig` plus the response metadata, and does
**not** write — you write with the one safe write path, `save()`:

```rust,no_run
use hl_peers::{Client, GossipConfig, PeerQuery, SaveOpts};

# async fn run(client: Client) -> hl_peers::Result<()> {
let resp = client
    .list_peers_as_gossip_config(&PeerQuery::mempool_servers().cc(["DE"]).try_new_peers(true))
    .await?;
println!("status {} api_version {:?} etag {:?}", resp.status, resp.api_version, resp.etag);
let cfg: &GossipConfig = &resp.body;
print!("{}", cfg.to_json());                       // canonical §6.3 bytes, to stdout
let saved = cfg.save("/home/you/override_gossip_config.json", &SaveOpts::default().with_backup(true))?;
for w in &saved.warnings { eprintln!("warning[{}]: {}", w.code, w.message); }
# Ok(()) }
```

The server emits `root_node_ips, try_new_peers, chain`; `to_json()` re-renders
in the canonical order, byte-identical across the four language clients. The
server never sends an empty root list — it answers `400 empty-root-list`.

### Build one from a query, client-side

```rust,no_run
use hl_peers::{Client, Geo, MergeOpts, PeerQuery, UpdateOpts};
# async fn run(client: Client) -> hl_peers::Result<()> {
let res = hl_peers::update_gossip_config(
    &client,
    &PeerQuery::new().cc(["DE"]),
    None,                       // no path: render only
    &UpdateOpts::default(),
).await?;
print!("{}", res.json);         // always present, even when nothing is written
# Ok(()) }
```

### Update a file in place

```rust,no_run
# use hl_peers::{Client, PeerQuery, UpdateOpts};
# async fn run(client: Client) -> hl_peers::Result<()> {
let path = hl_peers::GossipConfig::default_config_path().unwrap();
let res = hl_peers::update_gossip_config(&client, &PeerQuery::tier1(), Some(&path), &UpdateOpts::default()).await?;
for w in &res.warnings { eprintln!("warning[{}]: {}", w.code, w.message); }
# Ok(()) }
```

### Add / remove a root, offline

```rust
use hl_peers::{GossipConfig, SaveOpts};

let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
cfg.add_root_ip("192.0.2.11");      // duplicate add is a no-op, not an error
cfg.remove_root_ip("192.0.2.10");   // absent remove is a no-op
cfg.add_reserved_peer_ip("198.51.100.20");
assert_eq!(cfg.root_ips(), vec!["192.0.2.11"]);
```

🪤 Two shapes, one file: `root_node_ips` entries are `{"Ip": "…"}` objects with
a **capital `I`**; `reserved_peer_ips` entries are **bare strings**. Round-tripping
one shape through the other silently produces a file the node will not accept,
so `validate()` refuses both mistakes.

### Merge warnings survive

`merge_peers` / `from_peers` **return** their warnings *and* keep them on the
config, so `validate()`, `save()` and `update_gossip_config()` re-report them
unioned with the state-derived ones and de-duplicated by code. A caller who
ignored the merge's return value cannot lose a `truncated`. The next merge
replaces them; only `clear_warnings()` drops them — a successful `save` does not.

🔴 The warning codes are **independent**. 25 roots with `try_new_peers: false`
reports **both** `many_roots_gt_24` and `try_new_peers_false_multi_root`; a
length warning never suppresses the pinning one. The only mutually exclusive
pair is `many_roots_gt_24` / `many_roots_gt_64`.

### Dry-run diff

```rust,no_run
# use hl_peers::{GossipConfig, SaveOpts};
# fn run(cfg: GossipConfig) -> hl_peers::Result<()> {
let res = cfg.save("/home/you/override_gossip_config.json", &SaveOpts::default().with_dry_run(true))?;
print!("{}", res.diff);   // unified, 3 lines of context; nothing written
# Ok(()) }
```

### Pin IPs with `keep_existing`

```rust
use hl_peers::{GossipConfig, MergeOpts, MergeMode};

let mut cfg = GossipConfig::new("Mainnet", ["192.0.2.10"]);
let opts = MergeOpts::default()
    .with_mode(MergeMode::Replace)
    .with_keep_existing(["192.0.2.10"])   // always retained, placed first
    .with_exclude(["203.0.113.9"]);       // 🪤 exclude BEATS keep_existing
cfg.merge_peers(["203.0.113.1", "203.0.113.9"], &opts).unwrap();
assert_eq!(cfg.root_ips(), vec!["192.0.2.10", "203.0.113.1"]);
```

**`max_roots` defaults to 8**, not the upstream contract's ≤ 24 (D-D): a shorter
root list starts the node faster, and a longer one does not buy better peers. Pass `with_max_roots(24)` for the upstream default.

`save()` validates first (nothing is written on an error), no-ops on
byte-identical content, writes through a temp file **in the same directory**
with `fsync` + `rename`, preserves an existing file's mode and creates a new one
`0600` unless `SaveOpts::file_mode` says otherwise.

🔴 The permission option is **`file_mode`** (`--file-mode` on the CLI), never
`mode`. `mode` is the **merge strategy** (`replace` | `append` | `prepend`) and
nothing else, so the two cannot collide inside `UpdateOpts`.

---

## Feature flags

| feature | default | what it adds |
|---|---|---|
| `client` | ✅ | the async HTTP surface: `Client`, `PeerQuery`, `reqwest` (rustls, no default features) + `tokio` + `futures-core` |
| `blocking` | — | `BlockingClient`, a sync facade over the async one (`tokio/rt`) |
| `cli` | — | the `hl-peers` binary (`clap`) |
| `mock-server-tests` | — | the integration test that spawns `tools/mock_server.py` |
| *(none)* | — | `default-features = false` ⇒ `GossipConfig` only: `serde`, `serde_json`, `thiserror`, **no HTTP dependency**. For an operator on a box with no API access. |

**MSRV 1.88**, verified with `--all-features`. The floor comes from the
`client` dependency graph (`encoding_rs`/`icu_*` via `reqwest`); the
config-only build was verified separately on **1.82**.

`Cargo.lock` **is committed**: this crate ships a binary (`hl-peers`) as well as
a library, so a reproducible build of that binary is worth more than letting the
lock float.

---

## The network map

See the network map: <https://hypervisor.gg/network> — the live peer graph these endpoints serve.

```rust,no_run
use hl_peers::{Client, Connectable, NetworkMapOpts};

# async fn run(client: Client) -> hl_peers::Result<()> {
let graph = client
    .network_map_graph(&NetworkMapOpts::new().max_tier(2).connectable([Connectable::Yes]))
    .await?;
if graph.body.available {
    println!("{:?} nodes, {:?} edges", graph.body.node_count, graph.body.edge_count);
    for e in &graph.body.edges { println!("{} -> {} span {:?}", e.from, e.to, e.span); }
    for n in &graph.body.nodes { println!("{} tier {:?} city {:?}", n.id, n.tier, n.city); } // nodes are `Peer`s
    if let Some(cities) = &graph.body.cities {          // None = no city positions served
        for c in cities { println!("{} {} ({}, {}): {} peers", c.id, c.name, c.lat, c.lon, c.peers); }
        println!("{}", graph.body.attribution.as_deref().unwrap_or_default());
    }
}
let summary = client.network_map_summary(&NetworkMapOpts::new().serves_mempool([Some(true)])).await?;
println!("connectable {:?} mempool {:?} auction {:?}",
    summary.body.connectable, summary.body.serves_mempool, summary.body.auction);
# Ok(()) }
```

Options: `max_tier`, `show_unreached`, `hide_unreachable`, `connectable`
(closed enum), `cc`, `operator`, `label`, `serves_mempool`,
`auction` and `network` (testnet, beta — see below), typed and validated exactly as on `PeerQuery`. Show `attribution`
wherever you show the map's geo. Same ETag cache, error mapping,
rate-limit and `api_version` exposure as `/peers`; `available: false` is
returned as-is.

---

## Testnet (beta)

Every route takes an optional `network` — `Network::Mainnet` |
`Network::Testnet` (wire `mainnet` | `testnet`). Set it per call, or once on the
client; a per-call value wins. **Unset sends nothing**, so the server reads
mainnet and a mainnet URL is byte-identical to before.

### List testnet peers and their hardfork versions

```rust,no_run
use hl_peers::{Client, GetPeerOpts, Network, NetworkMapOpts, PeerQuery};

# async fn run() -> hl_peers::Result<()> {
// Per call:
let client = Client::new()?;
let page = client.list_peers(&PeerQuery::new().network(Network::Testnet)).await?;
assert_eq!(page.body.envelope.network.as_deref(), Some("testnet")); // None on mainnet
if page.body.envelope.available {
    for p in &page.body.peers {
        // `version` is the node's reported hardfork version on testnet
        // (an opaque string, e.g. "1550"); None when it did not report one.
        println!("{} {:?} {:?} {:?}", p.id, p.version, p.cc, p.asn);
    }
}

// Or once, for every call that does not set its own:
let testnet = Client::builder().network(Network::Testnet).build()?;
let peer = testnet.get_peer("192.0.2.50").await?;               // ?network=testnet
let graph = testnet.network_map_graph(&NetworkMapOpts::new()).await?;
let facets = testnet.facets(None).await?;                        // `version` facet = hardfork versions
let mainnet_peer = testnet
    .get_peer_with("192.0.2.10", &GetPeerOpts::new().network(Network::Mainnet))
    .await?;                                                     // per call wins
# let _ = (peer, graph, facets, mainnet_peer);
# Ok(()) }
```

`network` and `chain` are different questions: `network` picks which graph is
read; `chain` filters on what the server states (a testnet body states
`chain: "Testnet"`). `Network` parses case-sensitively — `"Testnet"` or
`"devnet"` is `Error::Invalid { parameter: "network", .. }` before any request.

Known gaps — testnet is **beta**, and the crate does nothing special about any of
them:

- a **small graph**, and **no session data** yet;
- `blocks_behind` is **approximate**;
- testnet **resets** often, and `available: false` for a while afterwards is
  normal — check `available` before reading the list;
- **operator-run nodes may be omitted**;
- a deployment that does not serve testnet answers `400 network-not-served`,
  surfaced as `Error::BadRequest` with `parameter == Some("network")` — no new
  error variant, so existing matches keep compiling.

CLI: `--network mainnet|testnet` on `query`, `facets`, `peer` and
`gossip update`.

---

## Rate limits, and anonymous vs key

Calling without a key is **anonymous** — a supported tier, not an error state.
A key that is present but invalid is a `401`; an absent key is not. The key is
sent as `Authorization: Bearer …` and is never logged, never put in a query
string and never included in an error message.

Every result and every error carries a parsed `RateLimit { limit, remaining,
reset, policy, retry_after, retry_after_raw }` from the `RateLimit-*` headers.
Each member is nullable; a missing or unresolvable header is `None`, **never**
`0` (anonymous 200s may carry only `RateLimit-Policy`). `reset` is **seconds
from now**, not an epoch. Results carry `api_version` (the `API-Version`
header, `"1"` today) and errors expose it via `err.api_version()` —
informational only; a breaking change ships under `/v2` paths.

🔴 **Retries are off by default** (`max_retries = 0`). A `429` raises
`Error::RateLimited` at once, carrying:

- `reset_at: Option<SystemTime>` — when to try again, an absolute UTC instant
  from the client's injectable clock: `now + retry_after`, else
  `now + RateLimit-Reset`, else `None`. Sleep until it; nothing needs to parse a
  header;
- `signup_url` — where a key raises the limit (`None` if the body had none);
- `retry_after`, `limit`, `remaining`, `reset`, `policy`, and the problem body.

```rust,no_run
use hl_peers::{Client, Error, PeerQuery};

# async fn run(client: Client) -> hl_peers::Result<()> {
match client.list_peers(&PeerQuery::tier1()).await {
    Err(Error::RateLimited { reset_at, signup_url, .. }) => {
        if let Some(at) = reset_at {
            let wait = at.duration_since(std::time::SystemTime::now()).unwrap_or_default();
            tokio::time::sleep(wait).await;         // your decision, not the library's
        }
        if let Some(url) = signup_url { eprintln!("a key raises the limit: {url}"); }
    }
    other => { other?; }
}
# Ok(()) }
```

Opt in to automatic retry with `.max_retries(3)`; the policy below then applies.

`Retry-After` may be a delay in seconds **or** an RFC 9110 HTTP-date, and this
crate handles both: all digits ⇒ seconds; otherwise it parses the IMF-fixdate
and computes `max(0, date - now)` against the **injectable clock**
(`ClientBuilder::clock`, the same `Clock` trait `save` uses for `.bak` names), so
tests never depend on wall time. A past date yields `0` — retry immediately, not
"absent". Anything that parses as neither is treated as absent and falls back to
backoff: no guessing, and no failed request over a bad header. The resolved
seconds are what `rate_limit.retry_after` exposes, and `retry_after_raw` keeps
the original string for diagnostics. The cap applies either way, so an HTTP-date
an hour out raises rather than blocking.

🔴 This crate hardcodes **no** limit numbers. The OpenAPI `429` example prose and
the design plan's tier table disagree (D-A), so all limit knowledge comes from
the headers at runtime and the `detail` string is surfaced verbatim and never
parsed. The provisional plan figures — anonymous ~20/min, free ~10 rps /
100 000 per month — live in the repo README and are marked provisional there.

### Caching: ETag / If-None-Match, and 304s are free

The ETag cache is **on by default**, per client instance, in memory, 64 entries
LRU, never on disk. A repeated request sends `If-None-Match: <stored etag>`;
the server's `304` is handled **transparently** — you get the stored body back
with `from_cache == true`, `status == 304`, and the rate-limit headers of the
304 itself. 🔴 **A `304` costs no quota unit**, keyed or anonymous, which is why
the cache is on and why conditional polling (`poll_peers`) is the thing to do.
`Cache-Control: max-age` is deliberately not honoured as a hard TTL, so the
client always revalidates. Supplying your own validator
(`list_peers_conditional`) returns `Conditional::NotModified` instead of a body.

---

## Errors

`Error` is one `thiserror` enum matching §5.1:

`RateLimited` · `FilterUnavailable` · `ChainNotServed` · `CursorExpired` ·
`BadRequest` · `Unauthorized` · `NotFound` · `Unavailable` · `Server` ·
`Transport` · `Decode` · `Invalid` · `Truncated`.

Rust has no subtyping, so the **400 family** is a predicate:
`err.is_bad_request()` is true for `BadRequest`, `FilterUnavailable`,
`ChainNotServed` and `CursorExpired`. Discrimination is on `problem.type`, by
suffix match on the path segment — never on `title` or `detail`. A non
problem+json error body still maps to the right variant by status alone (D-K),
with `problem = None` and a ≤ 512-byte body excerpt cut on a UTF-8 boundary.

Retries are **off by default** (D-Q). With `.max_retries(n)`: `429` (per
`Retry-After`, **capped at 60 s** — above the cap it raises rather than blocking
your thread for minutes, D-I), `5xx` including `503`, and transport faults,
with `min(500 ms · 2ⁿ, 30 s) · jitter(0.5…1.0)`.
🔴 **Never** a `4xx` other than `429`. The sleeper and jitter are injectable, so
tests assert on decisions and never on wall-clock delays.

`available: false` is **not** an error for a query — `list_peers` returns it
as-is and `iter_peers` stops without raising — but it **is** an error
(`Unavailable`) for `from_peers` / `update_gossip_config`, because the
alternative is emitting the crash-loop shape (D-G).

---

## CLI

```bash
cargo install --path rust --features cli    # or: cargo run --features cli --bin hl-peers -- …

hl-peers query --chain Mainnet --cc DE --output table
hl-peers query --serves-mempool true --cc DE             # owner query 1
hl-peers query --auction holds_slot,bidding --country JP
hl-peers query --cc DE --format gossip-config            # server-rendered config to stdout
hl-peers facets --chain Mainnet --serves-mempool true,null
hl-peers peer 192.0.2.10
hl-peers query --network testnet --output table          # testnet (beta)
hl-peers peer 192.0.2.50 --network testnet
hl-peers gossip show     --path ~/override_gossip_config.json
hl-peers gossip validate --path ~/override_gossip_config.json
hl-peers gossip update   --path ~/override_gossip_config.json --cc DE --dry-run
hl-peers gossip update   --path ~/override_gossip_config.json --cc DE --file-mode 0600
hl-peers gossip update   --cc DE --stdout > override_gossip_config.json
```

Global flags: `--base-url` (default `https://api.hypervisor.gg`), `--api-key`,
`--timeout`, `--max-retries` (default **0**), `--no-cache`, `--quiet`,
`--verbose`, `--json-errors`.

Exit codes: `0` ok · `1` client/validation · `2` usage · `3` rate limited ·
`4` unauthorized · `5` not found · `6` unavailable/server/transport.
🔴 There is no `--restart` / `--apply` flag, and there never will be.

---

## Tests

```bash
cargo test                                  # no network, no Python
cargo test --no-default-features            # the config-only build
cargo test --features mock-server-tests     # also spawns tools/mock_server.py
cargo run --example quickstart -- http://127.0.0.1:8931
cargo run --example gossip_offline --no-default-features
```

One generic runner covers all 76 cases in `../fixtures/gossip/cases` (it calls
`validate()` after the last operation and compares the warning **set**, as
`fixtures/README.md` requires); the HTTP behaviour tests replay all 35 fixtures
in `../fixtures/http` through the `Transport` seam, including each `429`'s own
`expect.retry_after_seconds` with `clock.now` injected, the §4.7 gossip-config
golden (`to_json()` **and** the bytes `save()` writes), and a check that `Peer`,
`PeerMempool`, `PeerAuction`, the envelope and the query parameters cover
exactly the field sets in `../spec/peer-api-v1.fields.json`. Those
directories live outside the package root, so a packaged `.crate` cannot run
them — the runner says so and skips rather than passing vacuously.
