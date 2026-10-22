# hl_peers (Elixir)

Query the Hyperliquid **Peer API** at `https://api.hypervisor.gg` by filter —
tier, sentry label, country, connectable, mempool service, gossip-auction
standing — and turn the answer into an hl-node `override_gossip_config.json`,
byte for byte identically to the TypeScript, Rust and Python clients in this
repo. Version **1.1.0**.

Two namespaces, always separate:

| namespace | nature | needs network? |
|---|---|---|
| `HlPeers`, `HlPeers.Client`, `HlPeers.Query` | the HTTP query surface | yes |
| `HlPeers.GossipConfig` | pure, offline, deterministic file algebra | **never** |

`HlPeers.GossipConfig` has no dependency on the HTTP layer: you can edit a
config on a box with no API access at all. Only `update_gossip_config/4` joins
the two.

## Install

From git, or as a path dependency:

```elixir
# mix.exs
{:hl_peers, github: "HypervisorLabs/hl-peers", sparse: "elixir"}
# or
{:hl_peers, path: "../hl-peers/elixir"}
```

A Hex release of `hl_peers` will follow.

Elixir ~> 1.15, OTP 26+. Deps: `jason ~> 1.4`, `req ~> 0.5`.

## Quickstart

```elixir
client = HlPeers.Client.new()          # https://api.hypervisor.gg, anonymous

{:ok, page} = HlPeers.list_peers(client, HlPeers.Query.tier1())
Enum.map(page.peers, & &1.id)

page.envelope.available   # check this before trusting an empty list
page.api_version          # "1" — the API-Version header
```

Everything answers `{:ok, term}` or `{:error, %HlPeers.Error{}}`. Each has a
`!` variant that raises the same struct.

Configuration (`HlPeers.Client.new/1`): `:base_url`
(`"https://api.hypervisor.gg"`), `:api_key`, `:timeout` (10 s per attempt),
`:max_retries` (**0** — auto-retry is opt-in), `:max_retry_after` (60 s),
`:min_poll_interval` (60 s), `:cache` (on, 64 entries, LRU), `:user_agent`
(`hl-peers-elixir/1.1.0`), `:user_agent_suffix`, `:adapter`, `:clock_fun`,
`:base_path` (`""` — the API serves plain paths at the host root; set it only
for a proxy that mounts the API under a prefix). `base_url` resolves explicit →
`HL_PEER_API_BASE_URL` → `https://api.hypervisor.gg`; `api_key` resolves
explicit → `HL_PEER_API_KEY` → anonymous. Every result and every error carries
`api_version` (the `API-Version` header; informational, never a failure).

## The owner's example queries

```elixir
alias HlPeers.Query

# 1. peers that serve the mempool AND are in Germany
{:ok, page} = HlPeers.list_peers(client, Query.mempool_servers() |> Query.cc(["DE"]))

# 2. peers with room for another inbound connection, in Singapore
{:ok, page} = HlPeers.not_full(client, cc: ["SG"])

# 3. tier 1 — one step from the network's sentry nodes
{:ok, page} = HlPeers.tier1(client)

# 4. the sentries themselves
{:ok, page} = HlPeers.sentries(client)

# anything else, composed
query =
  Query.new()
  |> Query.cc(["DE", "NL"])
  |> Query.state(["full"])
  |> Query.max_blocks_behind(5)
  |> Query.seen_within("1h")
  |> Query.limit(50)

{:ok, page} = HlPeers.list_peers(client, query)
```

🪤 `not_full` is `accepting=true`, which is exactly `connectable=yes` — **an
estimate of whether the peer has room for another inbound peer, not a guarantee; it can be wrong in both directions.**

🪤 `mempool_servers()` excludes peers whose `mempool.serves` is `nil` — **unknown**. "unknown" is not "does not serve"; ask for
`serves_mempool: [true, nil]` to include them.

🪤 The geo argument is **tagged** (`cc:` / `region:`), never a bare string.
`region` is published **only for cloud-hosted peers** (`provider:region`, e.g.
`aws:eu-central-1`, for cloud-hosted peers) and answers
`400 filter-unavailable` while no geo data is loaded, which this client surfaces
as an error and never swallows into an empty list. `cc` is the broad geo key.

🪤 `tier` is exact and `max_tier` is a ceiling — different questions, never
mapped onto each other. `tier: nil` on a peer means it was **never placed** in the peer graph;
it is not tier 0.

🪤 `sentries()` uses `label=sentry`, not `tier=0`. Equivalent today; different
questions.

### Every filter

| setter | wire | notes |
|---|---|---|
| `chain/2` | `chain` | `"Mainnet"` \| `"Testnet"` (closed) |
| `cc/2`, `country/2` | `cc`, `country` | lists, upper-cased; `country` is the server-side alias of `cc` (both ⇒ union) |
| `region/2`, `asn/2`, `role/2`, `label/2`, `version/2` | same | open sets, lists → CSV |
| `tier/2` | `tier` | exact, list; `nil` member → `null` (never placed) |
| `max_tier/2` | `max_tier` | a ceiling, ≥ 0 |
| `connectable/2` | `connectable` | `yes` / `no` / `unknown` (closed) |
| `accepting/2` | `accepting` | boolean |
| `state/2` | `state` | `responsive` / `open` / `full` / `syncing` / `listening` / `unreachable` / `unknown` (closed) |
| `serves_mempool/2` | `serves_mempool` | list of `true` / `false` / `nil` (closed) |
| `auction/2` | `auction` | list of `"holds_slot"` / `"bidding"` / `"bid_before"` / `nil` (closed) |
| `try_new_peers/2` | `try_new_peers` | only with `list_peers_as_gossip_config/3` |
| `max_blocks_behind/2`, `max_latency_ms/2` + `vantage/2` | same | ≥ 0; `max_latency_ms` needs `vantage` |
| `min_uptime/2` | `min_uptime` | a fraction in `[0, 1]` |
| `seen_within/2` | `seen_within` | `^[0-9]+[mhd]$` |
| `sort/2`, `limit/2`, `cursor/2`, `fields/2`, `format/2` | same | not sent unless set |

Mempool and auction, the two signals every peer carries:

```elixir
# unknown peers — the unknowns, not the "no"s
{:ok, page} = HlPeers.list_peers(client, Query.new(serves_mempool: [nil]))

# peers holding a gossip read-priority slot, or bidding for one
{:ok, page} = HlPeers.list_peers(client, Query.new(auction: ["holds_slot", "bidding"]))

for peer <- page.peers do
  {peer.id, peer.mempool.serves, peer.mempool.source, peer.auction.status, peer.auction.slot}
end
```

`peer.mempool` is a `%HlPeers.Peer.Mempool{serves, last_checked, verdict,
first_frame_ms, source}` and `peer.auction` a `%HlPeers.Peer.Auction{status,
slot, since, last_bid_at}` (timestamps as UTC `DateTime`). An all-`nil` struct
means **unknown**, and `serves: nil` is never `false`. `Peer` also carries the
geo fields `cc_confidence`, `cc_source`, `asn`, `org`, plus
`uptime` and `feeders`; the envelope carries `attribution`, the geo-data
licence line.

`HlPeers.facets/3` takes every filter above except `sort`, `limit`, `cursor`,
`fields`, `format` and `try_new_peers` (refused locally), and returns an open
map of facets — including `serves_mempool` and `auction` — plus
`geo_coverage`:

```elixir
{:ok, facets} = HlPeers.facets(client, Query.new(cc: ["DE"]))
HlPeers.Facets.available?(facets, "serves_mempool")
HlPeers.Facets.values(facets, "auction")    # {:ok, [%{value: "holds_slot", count: 9}, …]}
```

### Every page

```elixir
client
|> HlPeers.stream_peers(HlPeers.Query.new(limit: 50))
|> Stream.filter(&HlPeers.Peer.connectable?/1)
|> Enum.take(20)
```

Lazy, cursor-paged, and guarded: `:max_items` defaults to **10 000** and never
to unbounded. The whole directory is ~900 nodes, which is 18 anonymous pages at
`limit=50`.

🪤 A cursor belongs to the graph generation that issued it. Paging across a data refresh
boundary answers `400 cursor-expired` → `%Error{kind: :cursor_expired}`. This
client **never** restarts the walk for you, because that would mix two
generations into one result set. Catch it and start over.

## The network map

See the network map: <https://hypervisor.gg/network> — the live peer graph these endpoints serve.

```elixir
{:ok, graph} = HlPeers.network_map_graph(client, max_tier: 2)
graph.nodes                                 # [%HlPeers.Peer{}] — the /peers decoder
Enum.map(graph.edges, &{&1.from, &1.to, &1.span})
graph.tiers                                 # [%{tier: 0, count: 1}, …]

{:ok, summary} = HlPeers.network_map_summary(client)
summary.connectable                         # %{"yes" => 3, "no" => 1, "unknown" => 0}
summary.serves_mempool                      # %{"true" => 2, "false" => 1, "null" => 1}
graph.cities                                # [%HlPeers.NetworkMap.CityCluster{}] or nil
```

Filters: `:max_tier`, `:show_unreached`, `:hide_unreachable`, `:connectable`
(closed enum), `:cc`, `:operator`, `:label`, `:serves_mempool` / `:auction`
(closed enums, exactly as on the peers query), and `:network` (see
[Testnet (beta)](#testnet-beta)). `cities` and `attribution` are
`nil` when the producer serves no city positions (`nil` is not `[]`); with
cities, each node's `city` is its cluster id. Same ETag cache, error mapping,
rate-limit and `api_version` exposure as the peers calls; `available: false` is
returned as-is.

## Testnet (beta)

Every call takes a `network`: `"mainnet"` (the server's default) or
`"testnet"`. The atoms `:mainnet` / `:testnet` work too. It is a query field on
`list_peers/3`, `stream_peers/3`, `facets/3` and
`list_peers_as_gossip_config/3`, a filter on `network_map_graph/3` /
`network_map_summary/3`, and an option on `get_peer/3`. The client can also
carry a default, and a per-call value wins over it. Unset sends nothing, so a
mainnet request is byte-identical to one sent before `network` existed and
hits the same cache key.

List testnet peers and their hardfork versions:

```elixir
{:ok, page} = HlPeers.list_peers(client, network: "testnet")
page.envelope.network                       # "testnet" (nil on a mainnet body)
page.envelope.chain                         # "Testnet"
Enum.map(page.peers, &{&1.id, &1.version})  # [{"192.0.2.50", "1550"}, …]

# one peer, the graph, or a client that reads testnet by default
{:ok, %{peer: peer}} = HlPeers.get_peer(client, "192.0.2.50", network: "testnet")
{:ok, graph} = HlPeers.network_map_graph(client, network: "testnet")

testnet = HlPeers.Client.new(network: "testnet")
{:ok, facets} = HlPeers.facets(testnet)
```

On testnet a peer's `version` is the node's reported **hardfork version**, an
opaque string such as `"1550"` (`nil` when the node did not report one). On
mainnet it is always `nil`. Testnet rows carry the same geo/ASN fields (`cc`,
`asn`, `org`, `region`) as mainnet.

`network` is a closed, lower-case, case-sensitive enum. `"Testnet"` or
`"devnet"` is refused locally with `%Error{kind: :invalid, parameter: "network"}`
before any request is sent. `"Testnet"` is the spelling of `chain`, which
filters on what the server states. `network` picks which graph is read.

Known gaps while testnet is in beta:

- the graph is small;
- there is no session data yet;
- `blocks_behind` is approximate;
- testnet resets, so `available: false` for a while afterwards is normal (it is
  returned as-is, as on mainnet);
- operator-run nodes may be omitted;
- a deployment that does not serve testnet answers `400 network-not-served`,
  which surfaces as `%Error{kind: :bad_request, parameter: "network"}`. There is
  no new error kind, so match on `parameter == "network"`.

## Gossip config recipes

> 🔴 The node reads `override_gossip_config.json` **when it starts**. Writing this file does **not** steer a running node: the change takes
> effect at the node's next start. This library will **never** restart, signal, or
> otherwise disturb your node — that is the operator's decision, at the
> operator's moment. Restarting a node *to apply a config* while it is behind can
> make a lagging box worse.

> The node's file is `$HOME/override_gossip_config.json`.

**Build one from a query, print it, place it yourself** (the safest path):

```elixir
{:ok, result} = HlPeers.update_gossip_config(client, HlPeers.Query.tier1())
IO.write(result.json)
```

**Let the server render it** (`format=gossip_config`), then write with the one
safe writer:

```elixir
path = HlPeers.GossipConfig.default_config_path()

{:ok, result} =
  HlPeers.list_peers_as_gossip_config(client, Query.new(cc: ["DE"], try_new_peers: true))

HlPeers.GossipConfig.to_json(result.config)   # canonical key order, not the server's
{:ok, _} = HlPeers.GossipConfig.save(result.config, path, backup: true)
```

`list_peers_as_gossip_config/3` never writes; `result` also carries
`rate_limit`, `api_version`, `etag`, `from_cache` and `status`. `list_peers/3`
refuses `format: "gossip_config"` (that body has no `peers` array) and names
this helper.

**Update a file in place:**

```elixir
path = HlPeers.GossipConfig.default_config_path()

{:ok, result} =
  HlPeers.update_gossip_config(client, HlPeers.Query.new(cc: ["DE"]), path,
    max_roots: 8,
    mode: :replace,
    backup: true
  )

result.changed          # false when the bytes already matched — nothing was written
Enum.map(result.warnings, & &1.message)
```

**Pin your own boxes with `keep_existing`, exclude one you know is dead:**

```elixir
HlPeers.update_gossip_config(client, query, path,
  keep_existing: ["192.0.2.10"],   # always retained, placed first
  exclude: ["198.51.100.9"]        # 🪤 exclude beats keep_existing, with a warning
)
```

**Dry run — the unified diff, and not one byte written:**

```elixir
{:ok, result} = HlPeers.update_gossip_config(client, query, path, dry_run: true)
IO.puts(result.diff)
```

**Offline, no API at all:**

```elixir
alias HlPeers.GossipConfig

{:ok, config} = GossipConfig.load(path)

config
|> GossipConfig.add_root_ip("192.0.2.11")
|> GossipConfig.remove_root_ip("198.51.100.9")
|> GossipConfig.set_try_new_peers(true)
|> GossipConfig.save(path, backup: true)
```

`save/3` validates first (nothing is written on error), skips a write entirely
when the bytes are identical, writes through a temp file **in the same
directory** and renames atomically, and preserves an existing file's mode
(`file_mode:`, default `0600`, for a new one — 🔴 the permission option is
`:file_mode`; `:mode` is `merge_peers/3`'s `:replace | :append | :prepend`).

Warnings raised by a merge stay **pending** on the config, so `validate/1`,
`save/3` and `update_gossip_config/4` re-report them even if you ignored what
the merge returned; only `GossipConfig.clear_warnings/1` drops them.

### Why `max_roots` defaults to 8

A shorter root list starts the node faster, and a longer one does not buy better
peers. Pass `max_roots: 24` for the upstream default; above 24 a warning fires, above 64 it
escalates.

### 🔴 Never zero roots

An empty `root_node_ips` makes hl-node assert at start and crash-loop, with no
built-in fallback. `validate/1`, `save/3`, `merge_peers/3` and `from_peers/2`
all refuse it, and a filter that matched nothing is refused rather than written:

```elixir
{:error, %HlPeers.Error{kind: :invalid, parameter: "root_node_ips"}} =
  GossipConfig.new("Mainnet", []) |> GossipConfig.save(path)
```

The same guard is why `available: false` on a response — the producer could not
build a graph — raises `%Error{kind: :unavailable}` in
`update_gossip_config/4`, instead of being mistaken for "no peers matched".

### Two entry shapes in one file

🪤 `root_node_ips` holds `{"Ip": "192.0.2.10"}` objects — capital `I`, lowercase
`p`, load-bearing — while `reserved_peer_ips` holds **bare strings**. Mixing them
produces a file the node will not accept, so `validate/1` refuses both
confusions. Accessors always hand you bare strings.

## Rate limits, anonymous vs key

Anonymous is a **supported tier, not an error**; a key raises the page ceiling.
Set it explicitly or via `HL_PEER_API_KEY`; it is sent as
`Authorization: Bearer …` and is never logged, never put in a query string and
never included in an error message.

All limit knowledge comes from the `RateLimit-*` headers at runtime — this
client hardcodes **no** numbers, and never parses the prose in a `429` `detail`
(the figures quoted there disagree with the plan's own tier table; see D-A).
Every result and every error carries the parsed block:

```elixir
page.rate_limit.limit       # RateLimit-Limit — integer or nil (never 0 for "absent")
page.rate_limit.remaining   # RateLimit-Remaining
page.rate_limit.reset       # RateLimit-Reset: seconds from now, not an epoch
page.rate_limit.policy      # RateLimit-Policy, the raw string
```

A `429` is `%HlPeers.Error{kind: :rate_limited}` carrying `reset_at` — an
absolute UTC `DateTime` (`now + Retry-After`, else `now + RateLimit-Reset`, from
the client's injectable clock) — and `signup_url` from the problem body.
🔴 **Auto-retry is off by default** (`max_retries: 0`): the library never sleeps
behind your back. Either sleep until `reset_at` yourself:

```elixir
case HlPeers.list_peers(client, query) do
  {:error, %HlPeers.Error{kind: :rate_limited, reset_at: %DateTime{} = at}} ->
    Process.sleep(max(DateTime.diff(at, DateTime.utc_now(), :millisecond), 0))
    HlPeers.list_peers(client, query)

  other ->
    other
end
```

…or opt in with `HlPeers.Client.new(max_retries: 3)` (see Error handling).

### Caching — 304s are free

The ETag cache is on by default, per client instance, in memory. A repeat query
sends `If-None-Match`; the server's `304 Not Modified` is handled
transparently — you get the stored body back with `from_cache: true` and the
`304`'s own rate-limit headers — and 🔴 **a `304` costs no quota unit**, keyed
or anonymous. That is why the cache is on by default and why polling is cheap.
Pass your own `If-None-Match` in `headers:` and you get
`%HlPeers.NotModified{}` instead, because you asked the conditional question
yourself.

## Error handling

```elixir
case HlPeers.list_peers(client, query) do
  {:ok, page} ->
    page.peers

  {:error, %HlPeers.Error{kind: :filter_unavailable} = e} ->
    # e.filter is the parameter the producer does not collect — e.g. "region"
    {:error, e.filter}

  {:error, %HlPeers.Error{kind: :rate_limited} = e} ->
    # e.reset_at, e.retry_after, e.signup_url, e.rate_limit
    {:retry_at, e.reset_at}

  {:error, %HlPeers.Error{} = e} ->
    if HlPeers.Error.bad_request?(e), do: {:bad_query, e}, else: {:error, e}
end
```

Kinds: `:rate_limited`, `:filter_unavailable`, `:chain_not_served`,
`:cursor_expired`, `:bad_request`, `:unauthorized`, `:not_found`, `:unavailable`,
`:server`, `:transport`, `:decode`, `:invalid`, `:truncated`. The first four are
the 400 family — `Error.bad_request?/1` covers all of them. The removed
`mempool=` filter answers `400 filter-removed`, a plain `:bad_request`.

Retries are **opt-in** (`max_retries: n`). Once enabled: `429` (sleeping
`Retry-After`, capped at 60 s), `5xx`, and transport failures, with jittered
exponential backoff. `Retry-After` may be a delay in seconds or an HTTP-date; a
date is measured against the injectable clock (`Client.new(clock_fun: …)`) and
an unparseable value is treated as absent, so the backoff takes over.
🔴 Never a `4xx` other than `429`. A `Retry-After` **above** the cap raises
immediately rather than blocking your process for minutes (D-I).

## The poller

```elixir
{:ok, _pid} =
  HlPeers.Poller.start_link(
    client: client,
    query: HlPeers.Query.new(cc: ["DE"]),
    interval: 300_000,
    gossip_path: HlPeers.GossipConfig.default_config_path()
  )
```

Optional and supervised; nothing else in this library needs a process. It
refuses an interval below `min_poll_interval` (60 s) — polling faster than the data refreshes returns identical bytes and spends quota. It uses
the ETag cache, aligns to the envelope's `next_sweep_at` plus 5–15 s of jitter
when present, and 🔴 **never restarts your node**: a file it rewrites takes
effect at the node's next start, which is yours to schedule.

## Tests

```bash
mix test                      # offline: no network, no Python, no listening port
mix test --include integration  # also boots tools/mock_server.py on a free port
```

The gossip conformance suite is **one generic runner** over
`../fixtures/gossip/cases/*.json`; the HTTP tests replay `../fixtures/http`
through an injected adapter. See [`../docs/elixir.md`](../docs/elixir.md) for the
fuller guide, the API reference pointers, and the fixture defect this client
documents rather than reproduces.
