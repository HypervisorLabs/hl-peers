# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/). Per-package entries are in
`elixir/CHANGELOG.md`, `typescript/CHANGELOG.md`, `rust/CHANGELOG.md` and
`python/CHANGELOG.md`.

## [Unreleased]

- **Testnet (beta).** All four clients take an optional `network` (`mainnet` |
  `testnet`) on every route, per call or as a client-wide default; unset sends
  nothing, so mainnet requests are unchanged. Envelopes decode `network`;
  testnet rows carry `version` (the node's hardfork version) and the same
  geo/ASN fields as mainnet. `400 network-not-served` is a `BadRequest` with
  `parameter: "network"`. The CLIs take `--network`. Known gaps are listed in
  `docs/concepts.md` ("Networks").
- Contract: `CLIENT_SPEC.md` §10 D-U, the `Network` parameter in
  `spec/peer-api-v1.openapi.yaml`, six testnet fixtures (`*_testnet`,
  `error_400_network_not_served`) and the matching `tools/cross_check`
  programmes. No version bump; packages stay at 1.1.0.
- TypeScript example `typescript/examples/05-peer-management.ts`: the Peer API as
  part of your own peer management (scheduled candidate refresh, per-ASN and
  per-country diversity, checking the roots already in your file, one-at-a-time
  rotation with a floor and a backup, and poll timing with backoff). Executed
  against the fixtures by `test/examples.test.ts`. No library change; packages
  stay at 1.1.0.

## [1.1.0] — 2026-10-20 — Initial release

Client libraries for the Hyperliquid Peer API at `https://api.hypervisor.gg`, in
Elixir (`:hl_peers`), TypeScript (`hl-peers`), Rust (`hl-peers`) and Python
(`hl-peers`), all at 1.1.0 and all implementing one contract, `CLIENT_SPEC.md`.

- The query surface: `list_peers`, `iter_peers`, `get_peer`, `facets`,
  `network_map_graph` / `network_map_summary` and `list_peers_as_gossip_config`,
  with a typed query builder and the `tier1()`, `sentries()`, `not_full()` and
  `mempool_servers()` presets.
- Typed models for peers (including the `mempool` and `auction` signals and city
  clusters), envelopes, facets and the network map; unknown fields are kept.
- The `RateLimit-*` and `API-Version` headers on every result and error; a typed
  error taxonomy; an ETag cache; retries off by default.
- `GossipConfig`: an offline, deterministic editor for
  `override_gossip_config.json` with byte-identical output across the four
  languages, validation (including the refusal to write an empty root list) and an
  atomic `save`.
- An optional `hl-peers` CLI (TypeScript, Rust, Python).
- `spec/`: a redacted copy of the public OpenAPI document, the generated field
  list and the gossip-config JSON Schema.
- `fixtures/`: the language-neutral conformance suite (38 HTTP fixtures, 76 gossip
  cases), `tools/mock_server.py`, and `tools/cross_check`, the differential test
  that drives all four clients through one input set.
- MIT licence.
