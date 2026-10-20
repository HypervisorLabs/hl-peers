# Changelog — `hl-peers` (TypeScript)

## 1.1.0 — 2026-10-20 — Initial release

- `PeerApiClient` for `https://api.hypervisor.gg` (built-in `fetch`, zero runtime
  dependencies, ESM and CJS): `listPeers`, `iterPeers`, `getPeer`, `facets`,
  `pollPeers`, `listPeersAsGossipConfig`, `networkMapGraph`, `networkMapSummary`.
- An immutable `peerQuery()` builder with the presets `tier1()`, `sentries()`,
  `notFull({cc|region})` and `mempoolServers()`; closed-enum filters checked
  locally.
- Typed results: `Peer` (with `mempool`, `auction`, `city`), page, facets and
  network-map types with `CityCluster`; unknown keys preserved in `extra`.
- An ETag cache, `RateLimit-*` / `API-Version` exposure on every result and
  error, typed error classes, and opt-in retries (off by default).
- `GossipConfig`: a pure, offline editor for `override_gossip_config.json` with
  byte-exact serialization, `validate()`, `mergePeers()` and `fromPeers()`; the
  `hl-peers/gossip-fs` entry point adds an atomic `save()`, `load()` and
  `updateGossipConfig()`.
- The `hl-peers` CLI.
