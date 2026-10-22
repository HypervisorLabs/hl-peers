# hl-peers (Python)

Query the Hyperliquid Peer API (`https://api.hypervisor.gg`) by filter — tier,
sentry label, country, connectable, mempool service, auction standing — and
build or maintain an hl-node `override_gossip_config.json` from the result.

Two namespaces, kept apart on purpose:

| | needs the network? | |
|---|---|---|
| `PeerApiClient` | yes | the HTTP query surface |
| `GossipConfig` | **never** | pure, offline, deterministic file algebra |

`GossipConfig` imports nothing but the standard library, so it works on a box
with no API access and no packages at all. Only `update_gossip_config()` joins
the two.

> 🔴 **The node reads `override_gossip_config.json` when it starts.**
> Writing this file does **not** steer a running node: the change takes effect at
> the node's next start. This library will **never** restart, signal, or
> otherwise disturb your node — that is the operator's decision, at the
> operator's moment. Restarting a node *to apply a config* while it is behind
> can make a lagging box worse.

The node's file is `$HOME/override_gossip_config.json`.

---

## Install

```bash
pip install "hl-peers @ git+https://github.com/HypervisorLabs/hl-peers#subdirectory=python"
pip install -e python/                      # or from a checkout of this repo
pip install -e "python/[httpx]"             # plus the async client
```

The package is installed from the repository for now; a PyPI release will follow.

Python ≥ 3.9. **Zero required runtime dependencies** — node runners' boxes often
have a bare system Python, so the stdlib `urllib` path is first-class. The
optional `[httpx]` extra adds `AsyncPeerApiClient` and an httpx-backed sync
transport; nothing else changes.

## Configuration

| option | default | |
|---|---|---|
| `base_url` | `https://api.hypervisor.gg` | `argument → HL_PEER_API_BASE_URL → default` |
| `api_key` | `None` (anonymous) | `argument → HL_PEER_API_KEY → None`; sent as `Authorization: Bearer …`, never logged |
| `base_path` | `""` | routes are plain paths at the host root (`/peers`, `/network-map/graph`); set a prefix only behind a proxy |
| `max_retries` | **`0`** | automatic retry is **off**; opt in with e.g. `max_retries=3` |
| `timeout` | `10.0` s | per attempt |
| `cache` | in-memory ETag LRU, 64 entries | per client instance, never on disk |
| `network` | `None` | `"mainnet"` \| `"testnet"` (beta): the default for every call that sets none; `None` sends nothing (mainnet). See [Testnet (beta)](#testnet-beta) |

Every result and every error carries `api_version` — the `API-Version` response
header, `"1"` today. It is informational: the client never fails on a different
value (a breaking change ships under `/v2` paths).

To develop against the fixtures instead of the live host, run the mock server:

```bash
python3 tools/mock_server.py --port 8931 &
export HL_PEER_API_BASE_URL=http://127.0.0.1:8931
hl-peers query --cc DE --output ips
```

---

## Quickstart

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery

client = PeerApiClient()  # https://api.hypervisor.gg, or HL_PEER_API_BASE_URL

page = client.list_peers(PeerQuery().cc("DE"))
for peer in page:
    print(peer.id, peer.tier, peer.cc, peer.connectable, peer.mempool.serves)

# the envelope is never hidden from you
assert page.available is True        # False = the producer could not build a graph
assert page.stale is False           # check `available` FIRST
assert page.api_version == "1"       # the API-Version header
print(page.attribution)              # licence credit for the geo/ASN data
print(page.rate_limit.remaining, "quota units left in this window")
```

Async, with the `[httpx]` extra:

```python
import asyncio
from hl_peers.aio import AsyncPeerApiClient
from hl_peers import PeerQuery

async def main():
    async with AsyncPeerApiClient() as client:
        async for peer in client.iter_peers(PeerQuery().cc("DE"), max_items=500):
            print(peer.id)

asyncio.run(main())
```

### Paging

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery, CursorExpired

client = PeerApiClient()
iterator = client.iter_peers(PeerQuery().limit(2).sort("tier"), max_items=10_000)
try:
    ips = [peer.id for peer in iterator]
except CursorExpired:
    # 🪤 A cursor is bound to the graph generation that issued it. The library
    # never restarts the walk for you — that would mix two generations into one
    # result set, which is exactly what the server refused to do. Start over.
    ips = [peer.id for peer in client.iter_peers(PeerQuery().limit(2).sort("tier"))]
print(len(ips), "peers;", "truncated" if iterator.truncated else "complete")
```

The whole directory is ~900 nodes — 18 anonymous pages at `limit=50`.
`max_items` defaults to **10 000** and is a hard guard; pass
`on_truncate="raise"` to get a `Truncated` exception instead of a silent stop.

---

## Filters

Every parameter of `GET /peers` is a method on the immutable `PeerQuery`
builder; each returns a new query. CSV parameters take several arguments, a
list, or a pre-joined string, and are serialized lexicographically with the
comma encoded as `%2C`, so two clients produce the same URL and CDN cache key.

| method | wire | notes |
|---|---|---|
| `chain("Mainnet")` | `chain` | closed: `Mainnet` \| `Testnet` |
| `cc("DE", "NL")` / `country(…)` | `cc` / `country` | upper-cased; the broad geo key. `country` is a server-side alias (both ⇒ union) |
| `region("aws:eu-central-1")` | `region` | `provider:region`, **cloud-hosted peers only** |
| `asn("AS64500")` | `asn` | |
| `tier(1, None)` | `tier` | **exact**; `None` selects nodes that were not placed |
| `max_tier(2)` | `max_tier` | a **ceiling** — `max_tier(1)` also returns tier 0 |
| `role(…)`, `label("sentry")`, `version(…)`, `fields(…)` | | open sets, never validated locally |
| `connectable("yes", "unknown")` | `connectable` | closed: `yes` \| `no` \| `unknown` |
| `accepting(True)` | `accepting` | exactly `connectable=yes` |
| `state("responsive", "open")` | `state` | closed: `responsive` `open` `full` `syncing` `listening` `unreachable` `unknown` |
| `serves_mempool(True, None)` | `serves_mempool` | closed: `True` \| `False` \| `None`; **`None` = unknown, not `False`** |
| `auction("holds_slot", "bidding")` | `auction` | closed: `holds_slot` `bidding` `bid_before` `None` (never seen) |
| `max_blocks_behind(5)`, `min_uptime(0.95)` | | `min_uptime` within `[0, 1]` |
| `max_latency_ms(80).vantage("eu")` | | `max_latency_ms` requires `vantage` |
| `seen_within("1h")` | | `^[0-9]+[mhd]$` |
| `sort("tier")`, `limit(50)`, `cursor(…)` | | no `sort` is sent unless you set one; `limit` is never clamped locally |
| `format("gossip_config")`, `try_new_peers(True)` | | use `list_peers_as_gossip_config` (below) |

Client-side validation is deliberately thin: closed enums (case-sensitive), the
numeric bounds and the `seen_within` pattern raise `Invalid` before anything is
sent; everything else goes to the server, whose `400` names the problem.

```pycon
>>> from hl_peers import PeerQuery, mempool_servers
>>> PeerQuery().cc("de", "nl").serves_mempool(True, None).to_query_string()
'cc=DE%2CNL&serves_mempool=true%2Cnull'
>>> mempool_servers().cc("DE").auction("holds_slot").to_query_string()
'auction=holds_slot&cc=DE&serves_mempool=true'

```

### Mempool and auction signals

Every peer carries two typed objects, `peer.mempool`
(`serves, last_checked, verdict, first_frame_ms, source`) and `peer.auction`
(`status, slot, since, last_bid_at`). An all-`None` object means **unknown**:
`mempool.serves is None` is *unknown*, which is not "does not serve".
Timestamps are ISO-8601 strings, like every other timestamp on `Peer`.

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery, mempool_servers

client = PeerApiClient()

# serves the mempool AND in Germany.
# 🔴 peers with serves: None (unknown) are EXCLUDED by serves_mempool=true.
for peer in client.list_peers(mempool_servers().cc("DE")):
    print(peer.id, peer.mempool.verdict, peer.mempool.first_frame_ms, peer.mempool.source)

# the unknowns, asked for explicitly
unknown = client.list_peers(PeerQuery().serves_mempool(None))
assert all(p.mempool.serves is None for p in unknown)

# standing in the gossip read-priority auction
for peer in client.list_peers(PeerQuery().auction("holds_slot", "bidding")):
    print(peer.id, peer.auction.status, peer.auction.slot, peer.auction.last_bid_at)
```

## Four common queries

```python
# runnable: mock
from hl_peers import PeerApiClient, mempool_servers, tier1, sentries, not_full

client = PeerApiClient()

# 1. serves the mempool AND in Germany
de_mempool = client.list_peers(mempool_servers().cc("DE"))

# 2. peers with a free inbound slot, in Singapore
sg_open = client.list_peers(not_full(cc="SG"))

# 3. tier 1 — EXACT, not a ceiling
one_hop = client.list_peers(tier1())

# 4. the sentry identity set, by label
sentry_set = client.list_peers(sentries())

print(de_mempool.ips(), sg_open.ips(), one_hop.total, sentry_set.total)
```

Five traps worth stating out loud:

* **`tier` is exact, `max_tier` is a ceiling.** `max_tier(1)` also returns
  tier 0. The library never maps one to the other.
* **`sentries()` uses `label=sentry`, not `tier=0`.** They are different
  questions and may give different answers.
* **`not_full()` is an estimate, not a guarantee.** It is `accepting=true`,
  which is exactly `connectable=yes`: an estimate of whether the peer has room
  for another inbound peer. It can be wrong in both directions.
* **The geo argument is tagged**: `not_full(cc="DE")` or
  `not_full(region="aws:eu-central-1")`, never a bare string. 🪤 `region` is
  published **only for cloud-hosted peers** (the cloud provider's region)
  and answers `400 filter-unavailable` while no geo data is loaded — raised as
  `FilterUnavailable`, never swallowed into an empty list. `cc` is the broad
  geo key.
* **`mempool_servers()` excludes the unknown.** `serves: None` means unknown,
  not "does not serve"; add `serves_mempool(True, None)` to include them.

### Three-state fields are never folded

`connectable` has three states and `unknown` means *we never observed this* —
not `no`, not `False`. The boolean convenience is named for the positive
confirmation only:

```python
# runnable: mock
from hl_peers import PeerApiClient

page = PeerApiClient().list_peers()
peer = next(p for p in page if p.id == "198.51.100.7")
assert peer.connectable == "yes"
assert peer.is_connectable is True        # excludes `unknown`, by name
assert peer.tier is None or isinstance(peer.tier, int)  # None = not placed
```

`tier` is the distance from the network's sentry nodes in the peer graph (tier
0 = sentries). `tier: None` means **this node was not placed**. It is not tier 0,
and the library never substitutes a sentinel.

### Check before you filter

```python
# runnable: mock
from hl_peers import PeerApiClient

facets = PeerApiClient().facets()
assert facets.facet_available("cc") is True
assert facets.facet_available("asn") is False   # NOT COLLECTED, not "nothing matched"
print(facets.facets["tier"].counts())           # {'0': 61, '1': 137, '2': 604, 'null': 110}
print(facets.facets["serves_mempool"].counts()) # {'true': 143, 'false': 388, 'null': 381}
print(facets.geo_coverage)                      # loosely typed coverage stats, or None
```

`available: false` on a facet means the attribute is not collected; an empty
`values` list under `available: true` means it is collected and nothing matches.
The two are different answers and this library keeps them apart.

`facets(query)` takes **every filter** — the counts are over the filtered set.
`sort`, `limit`, `cursor`, `fields`, `format` and `try_new_peers` raise
`Invalid`: the endpoint does not take them, and you would believe they applied.

## Network map

See the network map: <https://hypervisor.gg/network> — the live graph these endpoints serve.

```python
# runnable: mock
from hl_peers import PeerApiClient

client = PeerApiClient()
graph = client.network_map_graph(max_tier=2)
print(graph.node_count, graph.edge_count, [(t.tier, t.count) for t in graph.tiers])
for edge in graph.edges:
    print(edge.from_, "->", edge.to, edge.span)   # `from` is a Python keyword
print(graph.nodes[0].id, graph.nodes[0].labels)   # nodes are ordinary Peer objects

print(graph.nodes[1].city)                        # a city-cluster id, or None
for city in graph.cities or []:                   # None unless city positions are served
    print(city.id, city.name, city.lat, city.lon, city.peers)
print(graph.attribution)                          # show it wherever the map is shown

summary = client.network_map_summary()
print(summary.connectable)                        # {'yes': 3, 'no': 1, 'unknown': 0}
print(summary.serves_mempool, summary.auction)    # bucket counts, zero-filled

mempool_map = client.network_map_graph(serves_mempool=[True], auction="holds_slot")
```

Options, all optional: `max_tier`, `show_unreached`, `hide_unreachable`,
`connectable` (closed enum), `cc`, `operator`, `label`, and
`serves_mempool` / `auction` — the same closed enums and spellings as the
`/peers` builder (`None` in a list is the literal `null`; a pre-joined CSV is
accepted), refused locally with `Invalid` when out of enum. `cities` and
`attribution` are `None` when the producer serves no city positions — distinct
from an empty list. `available: false` is returned as-is, never as an error.
`network` (`"mainnet"` | `"testnet"`) is a per-call option on both, see below.

---

## Testnet (beta)

The API serves the testnet peer graph on every route behind an optional
`network` parameter. Set it per query, per call, or once on the client:

| where | how |
|---|---|
| `/peers`, `/peers/facets` | `PeerQuery().network("testnet")` |
| `get_peer`, `network_map_graph`, `network_map_summary` | `network="testnet"` keyword |
| every call | `PeerApiClient(network="testnet")`; a per-call value wins |

Unset sends **no** `network` key, so a mainnet request is byte-identical to
what it was before and the server's default (mainnet) governs. The value is a
closed, case-sensitive enum: `"Testnet"`, `"TESTNET"` or `"devnet"` raise
`Invalid` (with `parameter == "network"`) before any request. `network` is not
`chain`: `network` picks which graph is read, `chain` (`Mainnet` | `Testnet`)
filters on what the server states, and a testnet body states `chain: "Testnet"`.

List testnet peers and their hardfork versions:

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery

client = PeerApiClient(network="testnet")         # every call reads the testnet graph
page = client.list_peers()
print(page.network, page.chain)                   # testnet Testnet
for peer in page.peers:
    # version: the node's reported hardfork version, an opaque string ("1550"),
    # or None when the node did not report one. Always None on mainnet.
    print(peer.id, peer.version, peer.cc, peer.asn, peer.org)

detail = client.get_peer("192.0.2.50")            # the client default applies
print(detail.peer.version)                        # 1550
graph = client.network_map_graph()
print(sorted({n.version for n in graph.nodes if n.version}))

# per query / per call: PeerQuery().network("mainnet"), get_peer(ip, network="mainnet")
mainnet = PeerApiClient().list_peers(PeerQuery().cc("DE"))  # no network sent at all
print(mainnet.network)                            # None — every mainnet body
```

Every result exposes `network`: `"testnet"` on a testnet body, `None` otherwise
(which means mainnet). Testnet rows carry the same geo/ASN fields as mainnet.

Known gaps while testnet is in beta — the client does nothing special about any
of them:

- the graph is **small**; expect a handful of peers, not hundreds;
- there is **no session data yet**, so the session-derived signals stay unknown;
- `blocks_behind` is **approximate**;
- testnet **resets**: `available: false` for a while after one is normal, and is
  returned as-is like any other `available: false`;
- operator-run nodes may be **omitted** from the testnet graph;
- a deployment that does not serve testnet answers `400 network-not-served`,
  raised as `BadRequest` with `parameter == "network"` (no new error class):

```python
from hl_peers import BadRequest

try:
    client.list_peers()
except BadRequest as error:
    if error.parameter == "network":
        ...  # testnet is switched off on this deployment
```

---

## Gossip recipes

Everything in this section is offline, deterministic and byte-exact — the same
input produces the same bytes in all four language clients.

### The server's own config: `list_peers_as_gossip_config`

The API can render the override file itself (`format=gossip_config`). This
helper sends your query with that format, parses the answer into a
`GossipConfig`, and hands it back with the response metadata — it does **not**
write. Write with the ordinary `save()`, the one safe write path:

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery, default_config_path

result = PeerApiClient().list_peers_as_gossip_config(PeerQuery().cc("DE"))
print(result.config.root_ips(), result.etag, result.api_version)
print(result.to_json(), end="")                 # canonical key order, not the server's
saved = result.config.save(default_config_path())   # validate, atomic rename, 0600
print("wrote" if saved.written else "no change", saved.path)
```

`list_peers()` refuses `format=gossip_config` with `Invalid` — that body has no
peer list, and an empty `PeerList` would read as a finding about the network.
`update_gossip_config()` (below) is the other way in: it builds the file
client-side from peer rows, with `keep_existing`, `exclude`, merge modes and
`max_roots`.

### Build a config from a query

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery, update_gossip_config

result = update_gossip_config(PeerApiClient(), PeerQuery().cc("DE"), max_roots=8)
print(result.json, end="")     # nothing was written: no path was given
for warning in result.warnings:
    print("warning:", warning.code)
```

`json` is always present even when nothing is written, so "generate to stdout
and let the operator place the file" is a first-class path.

### Update a file in place

```python
# runnable: mock
import os
from hl_peers import PeerApiClient, PeerQuery, default_config_path, update_gossip_config

path = default_config_path()          # $HOME/override_gossip_config.json
result = update_gossip_config(
    PeerApiClient(), PeerQuery().cc("DE"), path,
    max_roots=8,
    keep_existing=["203.0.113.9"],    # pinned: always kept, always first
    exclude=["198.51.100.20"],        # 🪤 exclude beats keep_existing, with a warning
    backup=True,
)
print("changed" if result.changed else "no change", result.written_path)
assert os.path.exists(path)
```

The write is atomic: a temp file **in the same directory**, fsynced, then
renamed over the target (a rename across filesystems is not atomic), with the
directory fsynced afterwards. An existing file's mode is preserved and never
widened; a new file is `0600` unless `file_mode=` (CLI: `--file-mode`) says
otherwise. 🪤 `file_mode` is permissions; `mode` is the merge strategy and
nothing else. Saving byte-identical content twice writes once —
the second call is a no-op, with no mtime change and no `.bak`.

### See the diff first

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery, default_config_path, update_gossip_config

result = update_gossip_config(
    PeerApiClient(), PeerQuery().cc("DE"), default_config_path(), dry_run=True
)
print(result.diff or "(no change)")   # nothing was written
```

### Edit a file with no API at all

```pycon
>>> from hl_peers import GossipConfig
>>> config = GossipConfig.new("Mainnet", ["192.0.2.10"]).set_try_new_peers(False)
>>> config.add_root_ip("198.51.100.7").has_root_ip("198.51.100.7")
True
>>> config.add_root_ip("198.51.100.7").root_ips()   # a duplicate add is a no-op
['192.0.2.10', '198.51.100.7']
>>> config.remove_root_ip("198.51.100.7").root_ips()
['192.0.2.10']
>>> config.add_reserved_peer_ip("198.51.100.20").reserved_peer_ips()
['198.51.100.20']
>>> print(config.to_json(), end="")
{
  "chain": "Mainnet",
  "root_node_ips": [
    {
      "Ip": "192.0.2.10"
    }
  ],
  "try_new_peers": false,
  "reserved_peer_ips": [
    "198.51.100.20"
  ]
}

```

🪤 Two entry shapes live in one file: `root_node_ips` entries are objects with
the single key `Ip` — capital `I`, lowercase `p` — and `reserved_peer_ips`
entries are **bare strings**. Round-tripping one shape through the other
silently produces a file the node will not accept, so the library refuses both
mistakes.

### The guard that matters

```pycon
>>> from hl_peers import GossipConfig, Invalid
>>> try:
...     GossipConfig.new("Mainnet", []).validate()
... except Invalid as error:
...     print(error.parameter, "|", error.reason[:46])
root_node_ips | refusing to emit an empty root_node_ips: hl-no

```

An empty or missing `root_node_ips` crash-loops the node: hl-node asserts a
non-empty list at start and there is no built-in fallback. The refusal
fires in `validate()`, in `save()`, in `merge_peers()` and in `from_peers()`,
and `update_gossip_config` raises rather than writing when your filter matched
nothing. Widen the filter, or pass `keep_existing`.

### Why `max_roots` defaults to 8

A short root list makes the node start faster, and a longer one does not
improve peering: dead entries in a long list slow every start. Pass `max_roots=24` for the upstream default; above
24 a warning fires, above 64 it escalates.

Warnings are a closed, stable set — `many_roots_gt_24`, `many_roots_gt_64`,
`chain_absent`, `try_new_peers_false_multi_root`, `truncated`,
`exclude_overrides_keep_existing` — and are never swallowed.

---

## Refreshing the file on a timer, without restarting the node

The file is inert until the next child start, so a timer that rewrites it is
safe **and does nothing by itself**. That is the point: you keep a good root
list on disk, ready for whenever the node next restarts on your schedule.

`/usr/local/bin/refresh-gossip-roots`:

```bash
#!/usr/bin/env bash
set -euo pipefail
# --backup keeps the previous file; the write is atomic either way, so a crash
# mid-write cannot leave a half-file the node would choke on at next start.
hl-peers gossip update \
  --path "$HOME/override_gossip_config.json" \
  --cc DE \
  --max-roots 8 --keep 203.0.113.9 --backup
```

`/etc/systemd/system/refresh-gossip-roots.service` and `.timer`:

```ini
[Unit]
Description=Refresh hl-node gossip roots (writes a file; never restarts the node)

[Service]
Type=oneshot
User=hluser
ExecStart=/usr/local/bin/refresh-gossip-roots
```

```ini
[Unit]
Description=Refresh hl-node gossip roots hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
```

Or as cron, with the same jitter discipline:

```cron
17 * * * * sleep $((RANDOM \% 900)); /usr/local/bin/refresh-gossip-roots >> ~/gossip-refresh.log 2>&1
```

🔴 **Safety notes, all load-bearing:**

* The node reads the file **only when it starts**. This timer never
  restarts anything, and neither does the library or the CLI — there is no
  `--restart` and no `--apply` flag.
* **Never zero roots.** `hl-peers gossip update` exits non-zero and leaves the
  file untouched if the filter matched nothing. Do not "fix" that with
  `|| echo '{}' > override_gossip_config.json`; an empty config is the
  crash-loop shape.
* Keep `--max-roots` small. Dead roots slow the **next** start, which is
  exactly the moment you least want to spend time.
* `--keep` pins IPs you trust regardless of what the filter returns; a run that
  would otherwise be empty then still has roots.

---

## Rate limits, anonymous and keyed

Anonymous is a **supported tier, not an error state**. Omitting a key is never a
`401`; a key that is present and invalid is.

All limit knowledge comes from the `RateLimit-*` headers at runtime — this
library hardcodes no quota numbers, and never parses the figures quoted in a
429's human `detail` (they are prose). Every result *and* every error carries
the parsed block as `rate_limit` (`limit`, `remaining`, `reset`, `policy`,
`retry_after`). A missing header is `None`, never `0`; `reset` is **seconds
from now**, not an epoch.

A `429` raises `RateLimited` straight away — **the client never sleeps behind
your back unless you ask it to**. The error tells you when to come back:

```python
# runnable: mock
import time
from datetime import datetime, timezone
from hl_peers import PeerApiClient, RateLimited

client = PeerApiClient()
try:
    page = client.list_peers()
    print(page.rate_limit.limit, page.rate_limit.remaining, page.rate_limit.reset)
except RateLimited as error:
    # reset_at: an aware UTC datetime = now + Retry-After (else now + RateLimit-Reset)
    if error.reset_at is not None:
        time.sleep(max(0.0, (error.reset_at - datetime.now(timezone.utc)).total_seconds()))
    print("a free key raises the ceiling:", error.signup_url)
```

### Caching: 304s are free

Conditional requests are on by default and **a `304` costs no quota unit**,
keyed or anonymous. The client stores `{etag, body}` per request (64 entries,
LRU, in memory, per instance, never on disk) and revalidates with
`If-None-Match`; a `304` is handled transparently — you get the stored body with
`from_cache=True` and the rate-limit headers from the 304 itself. The client
always revalidates (`Cache-Control: max-age` is not a TTL here). Pass
`if_none_match=` yourself and you get a `NotModified` result instead, because
then you asked the conditional question.

`poll_peers()` refuses an interval below 60 s — the data does not change that
often, so a faster poll returns identical bytes — and aligns to `next_sweep_at`
(when the data is next refreshed) plus a
per-instance jitter when the response carries it.

### Retries — opt in

`max_retries` defaults to **0**. Set it to opt in:

```python
client = PeerApiClient(max_retries=3)
```

Then retried: `429` (waiting `Retry-After`), `5xx` including `503`, and
transport failures, with `min(500ms * 2^n, 30s) * (0.5 + random()/2)` backoff.
**Never retried:** any other 4xx — a `400`, `401` or `404` is deterministic and
retrying it burns quota to receive the same answer. A `Retry-After` larger than
`max_retry_after` (default 60 s) **raises immediately** rather than blocking
your thread for minutes.

`Retry-After` may be seconds **or** an HTTP-date: a date is resolved against the
client's injectable clock (`PeerApiClient(clock=…)`) as `max(0, date - now)`, a
past date means `0`, and an unparseable value is treated as absent (back off).
`rate_limit.retry_after` is always the resolved seconds; `retry_after_raw` keeps
the header as sent.

## Errors

| exception | raised on |
|---|---|
| `RateLimited` | `429`, with `retry_after`, `reset_at`, `limit`, `remaining`, `reset`, `policy`, `signup_url` |
| `FilterUnavailable` | `400 …/filter-unavailable` — the attribute is not collected |
| `ChainNotServed` | `400 …/chain-not-served` |
| `CursorExpired` | `400 …/cursor-expired` |
| `BadRequest` | any other `400` (e.g. `filter-removed` for the old `mempool=` filter, or `network-not-served` with `parameter == "network"`) — and the **base class** of the three above |
| `Unauthorized` / `NotFound` | `401` / `404` |
| `Unavailable` | `503`, and an `available: false` envelope in a config-building path |
| `ServerError` / `TransportError` / `DecodeError` | other `5xx` / connect-read-TLS / bad body |
| `Invalid` | client-side validation, with `parameter` and `reason` |
| `Truncated` | `iter_peers` hit `max_items` with `on_truncate="raise"` |

All of them derive from `HlPeersError`, carry `rate_limit` and `api_version`,
and carry the raw `problem` object when the body was problem+json. Discrimination is on
`problem.type`, never on prose.

---

## CLI

```
hl-peers query   [--chain Mainnet] [--network mainnet|testnet] [--cc DE,NL] [--country JP] [--tier 1] [--max-tier 2]
                 [--label sentry] [--accepting] [--state responsive]
                 [--serves-mempool true,null] [--auction holds_slot,bidding]
                 [--sort tier] [--limit 50] [--all] [--max-items 10000]
                 [--format json|gossip-config] [--output json|table|ips]
hl-peers facets  [any query filter]
hl-peers peer    <ip> [--network mainnet|testnet]
hl-peers gossip  show|validate      --path ~/override_gossip_config.json
hl-peers gossip  add-root|remove-root <ip> --path … [--dry-run] [--backup] [--stdout]
hl-peers gossip  update --path … [query flags] [--max-roots 8]
                 [--mode replace|append|prepend] [--no-dedupe]
                 [--keep …] [--exclude …] [--chain Mainnet]
                 [--try-new-peers|--no-try-new-peers] [--n-gossip-peers 20]
                 [--file-mode 0600] [--backup] [--dry-run] [--stdout]

Global: --base-url URL --api-key KEY --timeout SECS --max-retries N (default 0)
        --no-cache --quiet --verbose --json-errors
```

`--network` (`mainnet` | `testnet`, beta) is accepted by `query`, `facets`,
`peer` and `gossip update`; unset sends nothing (mainnet).

`query --format gossip-config` prints the server-rendered override file in
canonical form, to stdout; it never writes.

```bash
hl-peers gossip update --path ~/override_gossip_config.json \
    --cc DE --max-roots 8 --dry-run
hl-peers gossip update --stdout --cc DE > override_gossip_config.json
```

Exit codes: `0` ok · `1` client/validation error · `2` usage · `3` rate limited ·
`4` unauthorized · `5` not found · `6` unavailable/server/transport. Warnings go
to stderr; `--quiet` may drop the informational ones but never the
`>24 roots`, `>64 roots`, `truncated` or `exclude beats keep_existing` ones.
After a successful write the CLI prints, to stderr:

```
wrote <path>. The node reads this file when it starts — the change
takes effect on the node's next start, which is yours to schedule.
```

---

## Development

```bash
cd python && uv venv .venv && . .venv/bin/activate && uv pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy --strict src
pytest                      # gossip conformance cases + HTTP fixtures + CLI + integration
python -m build && twine check dist/*   # build only — nothing is published
```

The conformance suite is `../fixtures/`; see `../CLIENT_SPEC.md` for the
normative behaviour and `../docs/python.md` for the fuller guide.
