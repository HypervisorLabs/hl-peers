# Changelog — `hl-peers` (Rust)

The contract is [`CLIENT_SPEC.md`](../CLIENT_SPEC.md).

## 1.1.0 — 2026-10-20 — Initial release

- `Client` (feature `client`, default) — the async Peer API client:
  `list_peers`, `iter_peers`, `get_peer`, `facets`,
  `list_peers_as_gossip_config`, `network_map_graph`, `network_map_summary`
  and `poll_peers`; a blocking facade behind feature `blocking`.
- `PeerQuery` — a typed, immutable query builder with the presets `tier1()`,
  `sentries()`, `not_full(Geo)` and `mempool_servers()`; closed enums are typed.
- Typed results: `Peer` (with `PeerMempool`, `PeerAuction`, `city`),
  `PeerList`, `Facets`, `NetworkMapGraph` / `NetworkMapSummary` /
  `CityCluster`, with `rate_limit` and `api_version` on every result and error.
  Public structs and enums are `#[non_exhaustive]`.
- An ETag cache, RFC 9457 problem+json error mapping (`Error`), and an opt-in
  retry policy (`max_retries`, default `0`) that honours `Retry-After` in
  seconds or as an HTTP-date.
- `GossipConfig` — a pure, offline editor for `override_gossip_config.json`
  (available with `default-features = false`): byte-exact serialization,
  `validate`, `merge_peers`, `from_peers` and an atomic `save`. It refuses an
  empty root list.
- `update_gossip_config`, which joins a query to a config file, and the
  `hl-peers` CLI (feature `cli`).
- Defaults: `https://api.hypervisor.gg`, plain paths at the host root, anonymous
  access, user agent `hl-peers-rust/<version>`.
