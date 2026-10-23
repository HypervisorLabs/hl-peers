# hl-peers

Client libraries for the **Hyperliquid Peer API** (`https://api.hypervisor.gg`) in
Elixir, TypeScript, Rust and Python — one contract, four languages, identical
behaviour. **Version 1.1.0**, for the live v1 API.

Query the peer graph by the things a node runner actually asks about — *tier-1
peers*, *sentries*, *peers in DE that serve the mempool*, *who holds a gossip
auction slot*, *not full in SG* — and turn the answer into a drop-in
`override_gossip_config.json` for `hl-node`.

**See the network map:** [hypervisor.gg/network](https://hypervisor.gg/network) — the
same peer graph these clients query, drawn live.

```bash
hl-peers gossip update --path ~/override_gossip_config.json \
    --cc DE --serves-mempool true --max-roots 8 --dry-run
```

---

## At a glance

| | |
|---|---|
| packages | `hl_peers` (Hex), `hl-peers` (npm), `hl-peers` (crates.io), `hl-peers` (PyPI); install from this repository for now — registry packages will follow |
| the base URL | `https://api.hypervisor.gg` (the default; override with `HL_PEER_API_BASE_URL`) |
| API paths | plain, at the host root: `/peers`, `/peers/{ip}`, `/peers/facets`, `/network-map/graph`, `/network-map/summary` — no `/api`, no `/v1` |
| tests | fixtures + `tools/mock_server.py` only; the default test run never touches the network |

```bash
export HL_PEER_API_KEY="hlp_…"      # optional — anonymous is a real tier
```

Try the clients against the mock:

```bash
python3 tools/mock_server.py --port 8931 &
export HL_PEER_API_BASE_URL=http://127.0.0.1:8931
curl -s "$HL_PEER_API_BASE_URL/peers?cc=DE&serves_mempool=true" | head
```

---

## Repository layout

| path | what |
|---|---|
| `CLIENT_SPEC.md` | 🔴 **the normative contract.** Read this before writing a line |
| `spec/` | the upstream public OpenAPI (copied and redacted, see `spec/SOURCE.md`), the generated field list, and the gossip-config JSON Schema |
| `fixtures/` | the language-neutral conformance suite (38 HTTP incl. a gossip-config golden, 76 gossip cases) |
| `tools/` | the stdlib mock server, the fixture generator and linter, the spec field-list generator, and `cross_check/` — the differential test that drives all four clients through one input set |
| `docs/` | concepts, the gossip-config guide, and one page per language |
| `elixir/` `typescript/` `rust/` `python/` | one client per language; see each directory's README |

---

## Quickstart per language

Install from a clone of [github.com/HypervisorLabs/hl-peers](https://github.com/HypervisorLabs/hl-peers).
Details in `docs/<lang>.md` and each directory's README.

| language | directory | install | entry point | `hl-peers` CLI | `format=gossip_config` helper |
|---|---|---|---|---|---|
| Elixir | [`elixir/`](elixir/) | `{:hl_peers, path: "../elixir"}` | `HlPeers.Client`, `HlPeers.GossipConfig` | — | ✓ |
| TypeScript | [`typescript/`](typescript/) | build, then `npm i file:../typescript` | `PeerApiClient`, `GossipConfig` | ✓ | ✓ |
| Rust | [`rust/`](rust/) | `hl-peers = { path = "../rust" }` | `hl_peers::Client`, `::GossipConfig` | ✓ | ✓ |
| Python | [`python/`](python/) | `pip install -e python/` | `hl_peers.Client`, `hl_peers.GossipConfig` | ✓ | ✓ |

The shape is the same everywhere:

```
client = Client(api_key=…)                                        # base_url defaults to https://api.hypervisor.gg
peers  = client.list_peers(Query().cc(["DE"]).serves_mempool([True]))   # one page
peers  = client.iter_peers(Query().auction(["holds_slot", "bidding"]))   # paginate
facets = client.facets(Query().cc(["DE"]))                        # counts, incl. serves_mempool / auction
config = client.list_peers_as_gossip_config(Query().tier([1]))    # server-rendered override file
config.save("~/override_gossip_config.json")                      # validated, atomic, 0600
graph  = client.network_map_graph(max_tier=2)
result = update_gossip_config(client, Query().cc(["DE"]),
                              path="~/override_gossip_config.json",
                              max_roots=8, dry_run=True)
```

Named presets for the questions people actually ask: `tier1()`, `sentries()`,
`not_full(cc=…)`, `mempool_servers()`. *Serves the mempool and in DE* is
`mempool_servers().cc(["DE"])`.

### Mempool and gossip-auction filters

| filter | values | on the peer |
|---|---|---|
| `serves_mempool` | `true` (a serve confirmed within the last 48 hours), `false`, `null` (unknown or served previously — **not** "no") | `mempool: {serves, last_checked, verdict, first_frame_ms, source, last_confirmed, last_attempted}` (`source` = `probe`: observed by our network probing) |
| `auction` | `holds_slot`, `bidding`, `bid_before`, `null` (never seen in the auction) | `auction: {status, slot, since, last_bid_at}` |

Both are closed enums (a typo is refused client-side). While the producer has no
data for a family, the server answers `400 filter-unavailable` and the clients
raise `FilterUnavailable` — never an empty list. Both filters work on
`/network-map/graph` and `/network-map/summary` too, and the summary counts them.

### Peer states and city clusters

- **`listening`** peer state (filterable): the peer accepts inbound connections;
  its other details are unknown.
- **City clusters**: where the producer serves city positions,
  `/network-map/graph` (and `/summary`) carry `cities` — `{id, name, cc, lat,
  lon, peers, tiers}`, the city's own point rounded to 0.1° — plus the combined
  `attribution`, and every peer carries `city` (its cluster id, or `null`). No
  per-peer coordinate is ever published.

### Testnet (beta)

Every route takes an optional `network`: `mainnet` (the default) or `testnet`.
Leave it unset and nothing changes: no `network` key is sent and the request is
byte-identical to before. Set it per call, or once on the client as a default
(`network="testnet"` in Python, `network: "testnet"` in TypeScript,
`network: :testnet` in Elixir, `.network(Network::Testnet)` in Rust; each
README has the exact spelling).

A testnet response has the same shape as a mainnet one, adds
`"network": "testnet"` and states `chain: "Testnet"`. Each testnet row carries
**`version`, the node's reported hardfork version** (for example `"1550"`;
`null` on mainnet), and the same geo/ASN profile as mainnet rows (`cc`,
`cc_confidence`, `cc_source`, `region`, `asn`, `org`). No operator labels and no
mempool or auction signals on testnet.

List testnet peers and their hardfork versions (Python; every client README has
the same example):

```python
from hl_peers import PeerApiClient, PeerQuery

client = PeerApiClient(network="testnet")
page = client.list_peers(PeerQuery())
for p in page.peers:
    print(p.id, p.version, p.cc, p.org)
```

🧪 **Beta, known gaps:** the testnet graph is small (a few dozen nodes, most of
which report few or no children); there is no session data yet;
`blocks_behind` is approximate; operator-run nodes may be omitted; testnet is
reset and upgraded often, so peers age out and a reset can leave the graph
`available: false` for a while. A deployment that does not serve testnet answers
`400 network-not-served`, which the clients raise as `BadRequest` with
`parameter == "network"` (`CLIENT_SPEC.md` §10 D-U) — never a mainnet answer.

---

## Rate limits and tiers

| tier | credential | rate | page size | notes |
|---|---|---|---|---|
| **anonymous** | none | 20 req/min per IP, **2 000/day** (launch settings, may be tuned) | ≤ 50 | a first-class tier, not an error state |
| **free** | `Authorization: Bearer hlp_…` | 10 rps, 100 000/month | ≤ 500 | [create an account](https://console.hypervisor.gg/users/register), then a key (365 days, ≤ 10 per account) |
| **paid** | same | per plan | per plan | |

🪤 **The clients hardcode none of these numbers.** Every client reads
`RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, `RateLimit-Policy` and
`Retry-After` off the wire and exposes them on **every** response, including
errors, together with the `API-Version` header. A `429` (problem+json with
`signup_url`) raises a typed `RateLimited` error carrying `retry_after` and an
absolute `reset_at`. **Auto-retry is off by default**; set `max_retries` to opt in
(429 honours `Retry-After` up to a 60 s cap, 5xx/transport back off with jitter,
other 4xx never retry).

Anonymous callers get the **same fields and the same freshness** as key holders.
What differs is rate and page size, never truthfulness.

**Do not poll faster than once a minute.** The data refreshes on its own schedule,
so a faster poll returns identical bytes and spends quota. The response carries
`next_sweep_at` (when the data is next refreshed) so you can align to it; clients ship a
`min_poll_interval` guard defaulting to 60 s; and a `304` from a conditional
request costs no quota unit, so the built-in ETag cache makes polling free.

---

## 🔴 Gossip-config safety notes

The generator writes a file whose failure mode is a **crash loop**, not a warning.
Four things every user of this library must know:

1. **An empty `root_node_ips` takes your node down.** The node cannot start
   without roots, and there is no built-in fallback. These libraries **refuse**
   to emit or save an empty list — including when your filter matched nothing.
   That refusal is the single most important guard in the whole project.
2. **Changes apply at the node's next start.** Writing the file does *not* steer
   a running node. This library will never restart, signal or otherwise disturb
   your node; when to restart is your decision, at your moment. Restarting a node
   *to apply a config* while it is behind can make a lagging box worse.
3. **Two entry shapes in one file.** `root_node_ips` entries are objects with a
   capital-`I` key — `{"Ip": "192.0.2.10"}` — while `reserved_peer_ips` entries are
   **bare strings**. Round-tripping one shape through the other produces a file the
   node will not accept. Both are pinned by fixtures.
4. **Short root lists are better.** `max_roots` defaults to **8**: a shorter root
   list starts faster, and a longer one buys nothing.

The node reads `$HOME/override_gossip_config.json`.

Where node behaviour is unverified, the libraries **refuse** rather than guess
(IPv6 is the main example) — see `docs/gossip-config.md`.

---

## Tune the host before you peer: hl-node-tools

[HypervisorLabs/hl-node-tools](https://github.com/HypervisorLabs/hl-node-tools) is
our companion repo of host tuning and operations scripts for running a Hyperliquid
node on your own box: kernel network settings, systemd unit shape, a connection
ceiling in front of the gossip port, disk retention, and restart hygiene. It needs
nothing beyond a shell and Python 3, and every setting carries a comment saying
what it does and why.

Run its network tuning (`sysctl/apply.sh`, then `sysctl/verify.sh`) **before** you
point your node at new peers. Its measurements found the TCP send window to be the
single highest-leverage setting: one connection's throughput is bounded by
`window / RTT`, and the effective window is the minimum of the two ends, so a
default-sized buffer on either side caps what a well-chosen peer can deliver.
Its README also explains why a connection ceiling, not a blanket block, belongs in
front of the gossip port.

---

## Contributing, security, licence

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md).
Licence: MIT — see [`LICENSE`](LICENSE). Every package manifest and the OpenAPI
copy in `spec/` say the same.
