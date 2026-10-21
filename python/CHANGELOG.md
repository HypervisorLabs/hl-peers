# Changelog — hl-peers (Python)

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
