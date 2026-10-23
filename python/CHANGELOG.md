# Changelog — hl-peers (Python)

## Unreleased

- Simplified the source fields; see field reference.
- **Testnet (beta)** — CLIENT_SPEC §10 D-U. A `network` option, the closed
  case-sensitive enum `"mainnet"` | `"testnet"`: `PeerQuery.network()` on
  `/peers` and `/peers/facets`, a `network=` keyword on `get_peer`,
  `network_map_graph` and `network_map_summary`, and a client-level default
  (`PeerApiClient(network=…)`, also on `AsyncPeerApiClient`); a per-call value
  wins. Unset sends no `network` key, so mainnet request URLs are unchanged. An
  out-of-enum value (`"Testnet"`, `"devnet"`) is `Invalid` with
  `parameter == "network"` before any request.
- Every result decodes the body's `network` (`"testnet"`, or `None` = mainnet):
  `PeerPage`, `PeerDetail`, `Facets` (via `Envelope.network`),
  `NetworkMapGraph` and `NetworkMapSummary`.
- `Peer.version` is documented as the node's hardfork version on testnet
  (an opaque string, `None` when not reported) and `None` on mainnet.
- `400 network-not-served` raises the existing `BadRequest` with
  `parameter == "network"`; no new error class.
- CLI: `--network mainnet|testnet` on `query`, `facets`, `peer` and
  `gossip update`.

## 1.1.0 — 2026-10-20 — Initial release

- `PeerApiClient` (stdlib `urllib`, zero runtime dependencies) and
  `AsyncPeerApiClient` (optional `[httpx]` extra) for `https://api.hypervisor.gg`:
  `list_peers`, `iter_peers`, `get_peer`, `facets`, `poll_peers`,
  `list_peers_as_gossip_config`, `network_map_graph`, `network_map_summary`.
- A typed, immutable `Query` builder with the presets `tier1()`, `sentries()`,
  `not_full(cc=…|region=…)` and `mempool_servers()`; closed-enum filters checked
  locally.
- Typed results: `Peer` (with `mempool`, `auction`, `city`), `PeerPage`,
  `PeerDetail`, `Facets`, `NetworkMapGraph` / `NetworkMapSummary` with
  `CityCluster`; unknown keys preserved in `extra`.
- An ETag cache, `RateLimit-*` / `API-Version` exposure on every result and
  error, a typed error hierarchy, and opt-in retries (off by default).
- `GossipConfig`: a pure, offline editor for `override_gossip_config.json` with
  byte-exact serialization, `validate()`, `merge_peers()`, `from_peers()` and an
  atomic `save()`; `update_gossip_config()` joins it to the client.
- The `hl-peers` CLI.
