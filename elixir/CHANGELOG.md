# Changelog — hl_peers (Elixir)

## Unreleased

- `serves` now reflects recent confirmations; see field reference. The mempool
  object decodes `last_confirmed` and `last_attempted`.
- the `split_client_blocks` query option for `format=gossip_config` (with `serves_mempool=true` the server
  defaults it to `true` and `try_new_peers` to `false`).
- Simplified the source fields; see field reference.
- **Testnet (beta)** — `network` (`"mainnet"` | `"testnet"`, or the atoms), a
  closed, case-sensitive enum (`CLIENT_SPEC.md` §10 D-U): a `HlPeers.Query`
  field for `list_peers/3`, `stream_peers/3` / `iter_peers/3`, `facets/3` and
  `list_peers_as_gossip_config/3`; a `network_map_graph/3` /
  `network_map_summary/3` filter; a `get_peer/3` option; and a client-level
  default, `HlPeers.Client.new(network: …)` (default `nil`), which a per-call
  value overrides. Unset sends nothing: mainnet URLs are unchanged.
- `network` (`String.t() | nil`) is decoded on `HlPeers.Envelope` (so on
  `PeerList`, `PeerEnvelope` and `Facets`) and on `NetworkMap.Graph` /
  `NetworkMap.Summary`. A testnet `Peer.version` is the node's hardfork version.
- An out-of-enum `network` is `Invalid{parameter: "network"}` before any
  request. `400 network-not-served` is the existing `:bad_request` with
  `parameter: "network"`. There is no new error kind.

## 1.1.0 — 2026-10-20 — Initial release

- `HlPeers` — the Peer API client: `list_peers/3`, `stream_peers/3` /
  `iter_peers/3`, `get_peer/3`, `facets/3`, `list_peers_as_gossip_config/3`,
  `network_map_graph/3` and `network_map_summary/3`, plus `HlPeers.Poller`.
- `HlPeers.Query` — a typed, immutable query builder with the presets
  `tier1/0`, `sentries/0`, `not_full/1` and `mempool_servers/0`, and the thin
  client-side validation of `CLIENT_SPEC.md` §2.2.
- Typed results: `Peer` (with `Peer.Mempool`, `Peer.Auction`, `city`),
  `PeerList`, `Facets`, `NetworkMap.Graph` / `Summary` / `CityCluster`, with
  `:rate_limit` and `:api_version` on every result and error.
- An ETag cache (a `304` is replayed transparently), RFC 9457 problem+json error
  mapping, and an opt-in retry policy (`:max_retries`, default `0`) that honours
  `Retry-After` in seconds or as an HTTP-date.
- `HlPeers.GossipConfig` — a pure, offline editor for
  `override_gossip_config.json`: byte-exact serialization, `validate/1`,
  `merge_peers/3`, `from_peers/2` and an atomic `save/3`. It refuses an empty
  root list.
- `HlPeers.update_gossip_config/4`, which joins a query to a config file.
- Defaults: `https://api.hypervisor.gg`, plain paths at the host root, anonymous
  access, user agent `hl-peers-elixir/1.1.0`.
