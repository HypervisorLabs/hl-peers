# The Rust client — a fuller guide

`hl-peers` implements [`CLIENT_SPEC.md`](../CLIENT_SPEC.md) for Rust. The
[crate README](../rust/README.md) is the quickstart; this is the guide for
people wiring it into node infrastructure.

Everything below is verified against `fixtures/` and `tools/mock_server.py`.
**Version 1.1.0**, for the live API at `https://api.hypervisor.gg`
(plain paths at the host root).

---

## 1. The shape of the crate

Two namespaces that never touch:

```
hl_peers::Client         — HTTP.  feature `client` (default)
hl_peers::GossipConfig   — files. always available, never any network
hl_peers::update_gossip_config  — the one helper that joins them
```

`GossipConfig` compiles with `default-features = false` and then pulls only
`serde`, `serde_json` and `thiserror`. That is deliberate: an operator editing
`override_gossip_config.json` on a box with no API access should not be made to
compile `reqwest`, and a crate that cannot reach the network cannot surprise
anyone by reaching it.

```toml
hl-peers = { path = "…/rust", default-features = false }   # config only
hl-peers = { path = "…/rust" }                             # + async client
hl-peers = { path = "…/rust", features = ["blocking"] }    # + sync facade
hl-peers = { path = "…/rust", features = ["cli"] }         # + hl-peers
```

---

## 2. Configuring the client

| option | builder method | default |
|---|---|---|
| base URL | `.base_url(…)` | `$HL_PEER_API_BASE_URL`, else `https://api.hypervisor.gg` (`DEFAULT_BASE_URL`) |
| route prefix | `.base_path(…)` | `DEFAULT_BASE_PATH` = `""` — plain paths at the host root (D-N); for proxies only |
| API key | `.api_key(…)` | `$HL_PEER_API_KEY`, else anonymous |
| timeout | `.timeout(…)` | 10 s, **per attempt**, connect + read |
| user agent | `.user_agent(…)` / `.user_agent_suffix(…)` | `hl-peers-rust/<ver>` |
| retries | `.max_retries(n)` | **0** — auto-retry is **off** (D-Q); opt in with e.g. `3` |
| `Retry-After` cap | `.max_retry_after(d)` | 60 s |
| poll floor | `.min_poll_interval(d)` | 60 s |
| ETag cache | `.cache_enabled(b)` / `.cache_capacity(n)` | on, 64 entries |
| transport | `.transport(Arc<dyn Transport>)` | `reqwest` + rustls |
| sleeper / jitter | `.sleeper(…)` / `.jitter(…)` | tokio sleep, xorshift jitter |
| clock | `.clock(…)` | system clock; resolves an HTTP-date `Retry-After` |

Both `base_url` and `api_key` are resolved **once**, at `build()`.
`.suppress_placeholder_warning(_)` is accepted and ignored.

🔴 With retries off, nothing in this crate sleeps unless you ask it to. A `429`
is `Error::RateLimited { reset_at, signup_url, retry_after, .. }` straight away:
`reset_at` is the absolute instant (`SystemTime`) to try again, computed from the
injectable clock as `now + retry_after`, else `now + RateLimit-Reset`, else
`None`. A `503`, other `5xx` or transport fault also raises on the first attempt.
`.max_retries(n)` restores §5.2's policy — `Retry-After` honoured up to
`.max_retry_after` (60 s; above it, raise), exponential backoff for `5xx` and
transport faults, never a retry on another `4xx`.

Every `Response<T>` carries `api_version` (the `API-Version` header) beside
`rate_limit`, `etag`, `from_cache` and `status`; errors expose it through
`err.api_version()`. It is informational — a different value is never an error.

`Client` is `Clone` and every clone shares one connection pool and one ETag
cache. Build it once per process and pass it around.

---

## 3. Reading a response honestly

The envelope rides on every result:

```rust,no_run
# use hl_peers::{Client, PeerQuery};
# async fn run(client: Client) -> hl_peers::Result<()> {
let page = client.list_peers(&PeerQuery::tier1()).await?;
let env = page.envelope();
if !env.available { /* the producer could not build a graph */ }
if env.stale      { /* older than the staleness bound */ }
println!("generated {:?}, next refresh {:?}", env.generated_at, env.next_sweep_at);
# Ok(()) }
```

🔴 Check `available` **before** `stale`: a dark subsystem reports both as false.
`available: false` is *not* an error and *not* "no peers matched" — it means the
producer could not build a graph at all, and the empty list that comes with it
is not a finding about the network.

On `Peer`:

- `tier: Option<i64>`. `None` means **this node was never placed in the peer graph**. It is
  not tier 0, not `-1`, not a sentinel. Tier is the distance from the network's sentry nodes in the peer graph; a third-party
  explorer's "hops to validator" is a different quantity.
- `connectable` is three-state and never folded: `Connectable::Unknown` is not
  `Connectable::No`.
- `mempool: PeerMempool { serves, last_checked, verdict, first_frame_ms,
  source }` and `auction: PeerAuction { status, slot, since, last_bid_at }` are
  typed. An all-`None` object means **unknown**; `mempool.serves == None`
  is *unknown*, not `false`. A missing or `null` object decodes to the
  all-`None` value, never an error, and unknown keys inside them are ignored.
- `mempool.verdict` is an open machine token (e.g. `serves`); decode it as a
  string, never validate it, and render an unknown token as plain text.
- `PeerState::Listening`: the peer accepts inbound connections; other details
  (tier, height) are unknown.
- `city: Option<String>` is the peer's city-cluster id when the
  producer serves city positions, else `None` — never a coordinate.
- Geo: `cc`, `cc_confidence`, `cc_source`, `asn`, `org` (open
  strings), `uptime`, `feeders`. `region` is set only for cloud-hosted peers;
  `version` is always `null`. The envelope's `attribution` is the geo-data
  notice to show with them.
- `state`, `role`, `labels` and `auction.status` are **open** when decoded. An
  unrecognised member decodes as `PeerState::Other(String)` /
  `AuctionStatus::Other(String)` / a plain string — never an error, never
  dropped. As **filters** the same enums are closed: `PeerQuery::validate`
  refuses `Other`.
- Any key the server adds later lands in `peer.extra` rather than being dropped.

`Facets` keeps "not collected" and "collected, nothing matched" apart:

```rust,no_run
# use hl_peers::Client;
# async fn run(client: Client) -> hl_peers::Result<()> {
let f = client.facets(None).await?;
match f.available("asn") {
    Some(false) => println!("asn is NOT COLLECTED — filtering on it is a 400"),
    Some(true) if f.facets["asn"].values.is_empty() => println!("collected, nothing matches"),
    _ => {}
}
# Ok(()) }
```

`facets()` takes every **filter** — the counts are over the filtered set — and
refuses `sort`, `limit`, `cursor`, `fields`, `format` and `try_new_peers` with a
client-side `Invalid`, because the endpoint would ignore them and you would
believe they applied. Facet names are an open map (`serves_mempool` and
`auction` included); `geo_coverage` is kept as a loose `serde_json::Value`.

### Mempool, auction and state filters

```rust
use hl_peers::{AuctionStatus, PeerQuery, PeerState};

let q = PeerQuery::mempool_servers()          // serves_mempool=true (excludes unknown)
    .country(["jp"])                          // alias of cc; upper-cased
    .auction([Some(AuctionStatus::HoldsSlot), None])
    .state([PeerState::Responsive]);
assert_eq!(
    q.to_query_string().unwrap(),
    "auction=holds_slot%2Cnull&country=JP&serves_mempool=true&state=responsive"
);
```

`extra_param(name, value)` sends a parameter this build does not model (a
filter the server added within v1); a modelled name is refused. A parameter the
server has removed answers `400 filter-removed` as `Error::BadRequest`.

### The network map

See the network map: <https://hypervisor.gg/network> — the live peer graph these endpoints serve.

`network_map_graph(&NetworkMapOpts)` and `network_map_summary(&NetworkMapOpts)`
hit `/network-map/graph` and `/network-map/summary`. Nodes decode with the same
`Peer` decoder; counts are `Option<i64>` (absent is not `0`); `available: false`
is returned as-is. Options are `max_tier`, `show_unreached`, `hide_unreachable`,
`connectable` (closed), `cc`, `operator`, `label`,
`serves_mempool` / `auction` (closed, as on `PeerQuery`), serialized like a
query. The graph carries `cities: Option<Vec<CityCluster>>` and `attribution`
when the producer serves city positions (`None` = absent, not empty); the summary
adds `serves_mempool` / `auction` count maps and the same `cities` /
`attribution` (CLIENT_SPEC §10 D-S). The producer's own nodes are ordinary rows
(`operator_nodes.count == None`, §10 D-T); the client filters nothing itself.

### `list_peers_as_gossip_config`

`list_peers` refuses `format=gossip_config` (D-R). The typed helper sends the
query with `format=gossip_config` (overriding the caller's `format`, keeping
`try_new_peers`), parses the body with `GossipConfig::parse`, and returns it in a
`Response` with `status`, `etag`, `from_cache`, `rate_limit` and `api_version`.
It never writes: call `save(path, &SaveOpts)` on the result, the one safe write
path. `to_json()` and the saved bytes are golden-tested against
`fixtures/http/peers_format_gossip_config.json`.

---

## 4. Injecting a transport

Everything the client sends goes through one trait, so fixtures replace the
network entirely:

```rust
use std::sync::Arc;
use hl_peers::{BoxFuture, Client, HttpRequest, HttpResponse, Transport};

#[derive(Debug)]
struct Canned;

impl Transport for Canned {
    fn execute<'a>(&'a self, _req: HttpRequest) -> BoxFuture<'a, hl_peers::Result<HttpResponse>> {
        Box::pin(async {
            Ok(HttpResponse::new(
                200,
                vec![("Content-Type".into(), "application/json".into())],
                br#"{"version":"v1","available":true,"stale":false,"chain":"Mainnet",
                     "count":0,"total":0,"next_cursor":null,"peers":[]}"#.to_vec(),
            ))
        })
    }
}

let client = Client::builder()
    .base_url("https://fixtures.invalid")
    .transport(Arc::new(Canned))
    .build()
    .unwrap();
```

The same seam carries `RecordingSleeper` and `NoJitter`, so a retry test asserts
on *decisions* — which statuses retry, how many attempts, that `Retry-After` was
read — and never on wall-clock delays, exactly as §9(3) requires.

---

## 5. Pagination

`iter_peers` returns a `PeerStream`, which implements `futures_core::Stream` and
also offers an inherent `next().await` so you need no `StreamExt` import.

- Each request after the first is the same query with `cursor` replaced;
  everything else is byte-identical.
- It terminates on `next_cursor == null`, an empty page, `available == false`,
  or `max_items`.
- `max_items` defaults to **10 000** and is a hard guard. On reaching it the
  stream ends and `truncated()` is true; with
  `.with_on_truncate(OnTruncate::Raise)` it yields `Error::Truncated { seen }`
  instead. There is no unbounded mode.
- It never deduplicates across pages and never reorders: the API already ranked
  the result with `sort`.
- `stream.envelope()` is how you tell "the walk ended because the graph was
  unbuildable" from "the walk ended because there were no more peers".

🪤 `400 cursor-expired` surfaces as `Error::CursorExpired` and the walk stops.
The intended handling is *catch it and start over* — this crate will not restart
the walk for you, because that would mix two graph generations into one result
set, which is exactly what the server refused to do.

---

## 6. The gossip config algebra

`GossipConfig` is a value you edit. Unknown keys survive parse → mutate →
re-render untouched, so a field the node gains tomorrow is not dropped by a
client built today.

```rust
use hl_peers::GossipConfig;

let mut cfg = GossipConfig::parse(
    r#"{"chain":"Mainnet","root_node_ips":[{"Ip":"192.0.2.10"}],"future_key":42}"#,
).unwrap();
cfg.set_try_new_peers(false);
assert!(cfg.to_json().contains("future_key"));
```

Serialization is byte-exact across all four language clients:

1. known keys first, in the fixed order `chain`, `root_node_ips`,
   `try_new_peers`, `reserved_peer_ips`, `n_gossip_peers`, `split_client_blocks`;
2. unknown keys after them, sorted by Unicode code point, recursively for
   nested objects; array order preserved;
3. 2-space indent, `": "` after a key;
4. `[]` and `{}` inline when empty, expanded otherwise — so a root entry renders
   across three lines, not as the one-line `{ "Ip": … }` the upstream prose shows;
5. exactly one trailing `\n`;
6. literal UTF-8, RFC 8259 minimal escaping;
7. integers with no decimal point or exponent.

This crate writes that out itself rather than leaning on `serde_json`'s pretty
printer, so the output cannot change because some unrelated crate in your
dependency graph switched `serde_json` to `preserve_order`.

### Setters

`add_root_ip` / `remove_root_ip` / `set_root_ips` / `has_root_ip` / `root_ips`,
the four `*_reserved_peer_ip*` equivalents, `set_chain`, `set_try_new_peers`,
`set_n_gossip_peers`, `set_split_client_blocks`, and an `unset_*` for each
optional key (Rust's spelling of §6.2's "set it to null").

Two behaviours to know:

- `set_n_gossip_peers` **refuses** anything outside `[8, 100]`, at both ends
  (D-C). Silently clamping would write a number you did not choose into a file
  you will read back later.
- `set_split_client_blocks(false)` **removes the key**, because the node's own
  serializer omits it when false — but an explicit `false` already in a parsed
  file is preserved by a round trip. Only the setter removes it.

Setters otherwise do not validate. `validate()` and `save()` are the gate, which
is why `parse()` accepts a file it would refuse to write.

### `validate()`

Returns the **first** error in a fixed order, so all four clients report the same
one for the same file: empty/absent `root_node_ips` → entry shape (`Ip`, capital
I, single key) → IPv4 validity (IPv6 refused, with the reason spelled out) →
duplicates per list → `chain` spelling → `n_gossip_peers` → the two booleans.

Warnings come back alongside `Ok` and are never fatal:

| code | fires when |
|---|---|
| `many_roots_gt_24` | more than 24 roots |
| `many_roots_gt_64` | more than 64 (instead of the above) |
| `chain_absent` | no `chain` key |
| `try_new_peers_false_multi_root` | `try_new_peers: false` with > 1 root |
| `truncated` | a merge dropped candidates at `max_roots` |
| `exclude_overrides_keep_existing` | an IP was in both lists; `exclude` won |

🔴 The codes are **independent**: every condition that holds fires, and a
`many_roots_*` warning does not suppress, absorb or outrank
`try_new_peers_false_multi_root`. They describe two unrelated facts — this list
is slow to boot, and this list may or may not pin — and swallowing one because
the other fired would hide the harder problem behind the easier one. The one
exception is `many_roots_gt_24` / `many_roots_gt_64`, which are mutually
exclusive.

Merge-time warnings (`truncated`, `exclude_overrides_keep_existing`) arise from
the merge rather than the resulting state, so `merge_peers`/`from_peers` both
**return** them and **retain** them on the config as pending warnings
(`pending_warnings()`). `validate()`, `save()` and `update_gossip_config()`
report the union with the state-derived warnings, de-duplicated by code. The
next merge replaces them; only `clear_warnings()` drops them, and a successful
`save` explicitly does not.

### `save()`

In order: validate (on error **nothing is written**); render; a byte-identical
target is a complete no-op (no write, no `.bak`, no mtime change); `dry_run`
returns a unified diff and writes nothing; optional timestamped `.bak` from an
injectable clock; then a temp file `.<basename>.<n>.tmp` **in the same
directory**, `fsync`, atomic `rename`, `fsync` of the directory. A temp file in
`/tmp` would be wrong — a rename across filesystems is not atomic. An existing
file's mode is preserved and never widened; a new file is `0600`, or
`SaveOpts::file_mode` if you set it.

🔴 The permission option is **`file_mode`**, never `mode`. `mode` is §6.5's
merge strategy and nothing else: one options bag in `update_gossip_config` would
otherwise let `0o600` be read as a merge strategy, or `"append"` as permissions.
The CLI flag is `--file-mode`.

🔴 `save` never restarts, signals or reloads anything.

### `merge_peers` / `from_peers`

Deterministic and order-preserving; the client does not re-rank.

```
replace : keep_existing ++ candidates
append  : keep_existing ++ existing ++ candidates
prepend : keep_existing ++ candidates ++ existing
then    : remove exclude → dedupe (first wins) → truncate to max_roots (head)
```

- A candidate that is not a valid IPv4 is an **error**, not a silent drop:
  dropping it would shrink your root list without telling you.
- `exclude` beats `keep_existing`, and the conflict is named in a warning.
- `dedupe: false` is only usable on already-distinct input; a duplicate makes
  `validate` reject the result.
- 🔴 An empty result raises rather than writing, and never falls back to the
  previous roots silently.

**Why `max_roots` is 8 and not 24** (D-D): a shorter root list starts the node
faster, and a longer one does not buy better peers. `with_max_roots(24)` opts
into the upstream default.

---

## 7. `update_gossip_config`

One round of work: collect peers (`max_roots × overfetch`, overfetch 4, so
`exclude`/`keep_existing`/validity filtering still leaves candidates) → refuse
on `available: false` **before touching any file** → load or create → merge →
validate → save unless `no_write`.

`UpdateResult::json` is always populated, so "render to stdout and let the
operator place the file" is a first-class path rather than an afterthought.

The `chain` for a fresh config comes from `opts.merge.chain`, else the response
envelope's `chain`, else it raises. It is **never** defaulted to `Mainnet`: a
chain that disagrees with the node's `--chain` stops the node from starting, and guessing is how that happens.

`update_gossip_config_from_peers` is the same work over a peer list you already
hold — no network, so you can generate configs from a cached response.

---

## 8. Polling

`poll_peers(query, interval, callback)` refuses an interval below
`min_poll_interval` (60 s), because polling faster than the data refreshes
returns identical bytes and spends quota. It uses the ETag cache, so an
unchanged poll costs no quota unit, and when `next_sweep_at` is present it
aligns to the data's own clock plus 5–15 s of per-instance jitter. The callback
returns `false` to stop.

---

## 9. Blocking and the CLI

`BlockingClient` drives the async client on a private current-thread runtime —
one implementation of every behaviour, so the two surfaces cannot drift. Do not
call it from inside an async runtime.

The `hl-peers` binary implements §8's flags verbatim, so one runbook works
against any of the four language clients. Warnings go to stderr always; the
`>24 roots`, `>64 roots`, `truncated` and `exclude beats keep_existing`
warnings are never suppressed, even under `--quiet`. After a successful write it
prints the reminder that the node reads the file when it starts.

---

## 10. Testing this crate

```bash
cargo test                               # lib + conformance + HTTP + fs + doctests
cargo test --no-default-features         # the config-only build
cargo test --features mock-server-tests  # + spawns tools/mock_server.py on a free port
cargo clippy --all-targets --all-features -- -D warnings
cargo fmt --all -- --check
cargo doc --no-deps --all-features
```

`tests/gossip_conformance.rs` is **one** interpreter over all 76 cases in
`fixtures/gossip/cases`, not one test per case; after the last operation it
calls `validate()` and compares the warning set, and it refuses a non-error case
that omits `expect.warnings`. `tests/http_behaviour.rs` replays all 38 fixtures
in `fixtures/http` through the `Transport` seam, drives each `429`'s
`expect.retry_after_seconds` with the fixture's own `clock.now` injected, checks
the §4.7 golden, and asserts the types cover exactly the field sets in
`spec/peer-api-v1.fields.json` (read at test time). Neither touches the
network. Both directories live outside the package root, so a packaged `.crate`
cannot run them; the runners say so and skip rather than passing vacuously.
