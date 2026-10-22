# Changelog — `hl-peers` (TypeScript)

## Unreleased

- **Testnet (beta), `network`** (CLIENT_SPEC §10 D-U): a closed
  `'mainnet' | 'testnet'` option (new `Network` type). Builder field
  `peerQuery().network(…)` on `listPeers` / `iterPeers` / `facets` /
  `listPeersAsGossipConfig`; per-call option on `getPeer` and
  `networkMapGraph` / `networkMapSummary`; client-level default
  `new PeerApiClient({ network })`, where a per-call value wins. Unset sends
  nothing, so mainnet requests are byte-identical to 1.1.0. An out-of-enum value
  is `InvalidError` with `parameter: "network"` before any request.
- Every envelope (`Envelope`, `NetworkMapEnvelope`) decodes `network`
  (`"testnet"` or `null`). `Peer.version` is documented as the hardfork version
  on testnet (`null` on mainnet).
- `400 network-not-served` is the existing `BadRequestError` with
  `parameter === "network"`; no new error class.
- CLI: `--network mainnet|testnet` on `query`, `facets`, `peer` and
  `gossip update`.
- `examples/06-testnet.ts`: list testnet peers and their hardfork versions.
- `examples/05-peer-management.ts`: candidate refresh, diversity, root checks,
  one-at-a-time rotation with a floor and backup, poll timing. Example only; no
  API change.

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
