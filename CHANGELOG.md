# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/). Per-package entries are in
`elixir/CHANGELOG.md`, `typescript/CHANGELOG.md`, `rust/CHANGELOG.md` and
`python/CHANGELOG.md`.

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
