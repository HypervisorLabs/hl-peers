# Documentation index

| page | what it covers |
|---|---|
| [concepts.md](concepts.md) | filters, tiers, the three-state enums, rate limits, caching |
| [gossip-config.md](gossip-config.md) | `override_gossip_config.json` — the file, the traps, the safety rules |
| [elixir.md](elixir.md) | Elixir quickstart and API tour |
| [typescript.md](typescript.md) | TypeScript quickstart and API tour |
| [rust.md](rust.md) | Rust quickstart and API tour |
| [python.md](python.md) | Python quickstart and API tour |

Normative material lives outside `docs/`:

- [`../CLIENT_SPEC.md`](../CLIENT_SPEC.md) — the behaviour every client implements.
  When a page here and `CLIENT_SPEC.md` disagree, the spec wins and the page is a bug.
- [`../spec/peer-api-v1.openapi.yaml`](../spec/peer-api-v1.openapi.yaml) — the wire contract.
- [`../spec/gossip-config.schema.json`](../spec/gossip-config.schema.json) — the file schema.
- [`../fixtures/README.md`](../fixtures/README.md) — the conformance-suite format.
- [`../tools/cross_check/`](../tools/cross_check/) — the differential test that drives all
  four clients through the same inputs and asserts they agree byte for byte.

---

## One concept, four spellings

The behaviour is identical; only the casing and the error mechanism are each
language's own (`CLIENT_SPEC.md` §0). Every row below is
the same method, transliterated. Use this table to read a runbook written against
one language while working in another.

### The HTTP query surface

| concept | Elixir | TypeScript | Rust | Python |
|---|---|---|---|---|
| the client | `HlPeers.Client.new/1` | `new PeerApiClient(opts)` | `Client::builder()` / `BlockingClient` | `hl_peers.Client(...)` |
| the query builder | `HlPeers.Query` | `PeerQuery` / `peerQuery()` | `PeerQuery` (aliased `Query`) | `PeerQuery` (aliased `Query`) |
| one page | `HlPeers.list_peers/3` | `client.listPeers(q)` | `client.list_peers(&q)` | `client.list_peers(q)` |
| paginate | `HlPeers.stream_peers/3` (alias `iter_peers/3`) | `client.iterPeers(q, opts)` | `client.iter_peers(&q, opts)` | `client.iter_peers(q, ...)` |
| eager paginate | `HlPeers.list_all_peers/3` | `client.listAllPeers(q, opts)` | `client.list_all_peers(&q, opts)` | `client.list_all_peers(q, ...)` |
| one peer | `HlPeers.get_peer/3` | `client.getPeer(ip)` | `client.get_peer(ip)` | `client.get_peer(ip)` |
| facets | `HlPeers.facets/3` | `client.facets(q)` | `client.facets(&q)` | `client.facets(q)` |
| poll | `HlPeers.Poller` | `client.pollPeers(...)` | `client.poll_peers(...)` | `client.poll_peers(...)` |
| presets | `Query.tier1/0`, `sentries/0`, `not_full/1` | `tier1()`, `sentries()`, `notFull({cc})` | `PeerQuery::tier1()`, `sentries()`, `not_full(Geo)` | `tier1()`, `sentries()`, `not_full(cc=…)` |
| route prefix | `Client.new(base_path: …)`, default `""` (`Client.default_base_path/0`) | `new PeerApiClient({ basePath })`, default `DEFAULT_BASE_PATH` = `''` | `.base_path(…)`, default `DEFAULT_BASE_PATH` = `""` | `PeerApiClient(base_path=…)`, default `DEFAULT_BASE_PATH` = `""` |
| mempool / auction filters | `Query.serves_mempool/2`, `Query.auction/2`, preset `Query.mempool_servers/0` | `.servesMempool([...])`, `.auction([...])`, `mempoolServers()` | `.serves_mempool(...)`, `.auction(...)`, `PeerQuery::mempool_servers()` | `.serves_mempool(...)`, `.auction(...)`, `mempool_servers()` |
| peer signals | `%Peer.Mempool{}`, `%Peer.Auction{}` | `PeerMempool`, `PeerAuction` | `PeerMempool`, `PeerAuction` | `PeerMempool`, `PeerAuction` |
| server gossip config | `HlPeers.list_peers_as_gossip_config/3` → `%GossipConfigResult{}` | `client.listPeersAsGossipConfig(q)` | `client.list_peers_as_gossip_config(&q)` | `client.list_peers_as_gossip_config(q)` |
| network map | `HlPeers.network_map_graph/3`, `network_map_summary/3` | `client.networkMapGraph(o)`, `networkMapSummary(o)` | `client.network_map_graph(&o)`, `network_map_summary(&o)` | `client.network_map_graph(...)`, `network_map_summary(...)` |
| city clusters | `%HlPeers.NetworkMap.CityCluster{}`, `peer.city`, `graph.cities` (nil when absent) | `CityCluster`, `peer.city`, `graph.cities` (null when absent) | `CityCluster`, `peer.city`, `graph.cities: Option<Vec<_>>` | `CityCluster`, `peer.city`, `graph.cities` (None when absent) |
| map mempool / auction filters | `network_map_graph(c, serves_mempool: …, auction: …)` | `networkMapGraph({ servesMempool, auction })` | `NetworkMapOptions` `.serves_mempool(…)`, `.auction(…)` | `network_map_graph(serves_mempool=…, auction=…)` |
| API-Version / 429 reset | `:api_version`, `%Error{reset_at: …}` | `apiVersion`, `RateLimitedError.resetAt` | `api_version`, `Error::RateLimited { reset_at, .. }` | `api_version`, `RateLimited.reset_at` |
| page result | `%HlPeers.PeerList{}` | `PeerListResult` | `PeerList` | `PeerPage` (aliased `PeerList`, `Page`) |
| envelope | `%HlPeers.Envelope{}` | `Envelope` | `Envelope` | `Envelope` |
| rate-limit block | `%HlPeers.RateLimit{}` | `RateLimit` | `RateLimit` | `RateLimit` |
| a 304 the caller asked for | `%HlPeers.NotModified{}` | `NotModifiedResult` | `Conditional::NotModified` | `NotModified` |

### The offline file algebra

`GossipConfig` is the same name in all four. Its methods transliterate:

| concept | Elixir | TypeScript | Rust | Python |
|---|---|---|---|---|
| parse / load | `parse/1`, `load/1`, `load_or_new/2` | `GossipConfig.parse`, `load`, `loadOrNew` ¹ | `GossipConfig::parse`, `load`, `load_or_new` | `GossipConfig.parse`, `load`, `load_or_new` |
| build | `new/2`, `from_peers/2` | `GossipConfig.new`, `fromPeers` | `GossipConfig::new`, `from_peers` | `GossipConfig.new`, `from_peers` |
| roots | `root_ips/1`, `has_root_ip?/2`, `add_root_ip/2`, `remove_root_ip/2`, `set_root_ips/2` | `rootIps`, `hasRootIp`, `addRootIp`, `removeRootIp`, `setRootIps` | `root_ips`, `has_root_ip`, `add_root_ip`, `remove_root_ip`, `set_root_ips` | `root_ips`, `has_root_ip`, `add_root_ip`, `remove_root_ip`, `set_root_ips` |
| reserved peers | `reserved_peer_ips/1`, `add_reserved_peer_ip/2`, … | `reservedPeerIps`, `addReservedPeerIp`, … | `reserved_peer_ips`, `add_reserved_peer_ip`, … | `reserved_peer_ips`, `add_reserved_peer_ip`, … |
| other fields | `set_chain/2`, `set_try_new_peers/2`, `set_n_gossip_peers/2`, `set_split_client_blocks/2`, `unset_split_client_blocks/1` | `setChain`, `setTryNewPeers`, `setNGossipPeers`, `setSplitClientBlocks`, `unsetSplitClientBlocks` | `set_chain`, `set_try_new_peers`, `set_n_gossip_peers`, `set_split_client_blocks`, `unset_split_client_blocks` | `set_chain`, `set_try_new_peers`, `set_n_gossip_peers`, `set_split_client_blocks`, `unset_split_client_blocks` |
| merge | `merge_peers/3` | `mergePeers` | `merge_peers` | `merge_peers` |
| render | `to_json/1` | `toJson` | `to_json` | `to_json` |
| check | `validate/1` | `validate` | `validate` | `validate` |
| write | `save/3` ¹ | `save(config, path, opts)` ¹ | `save` | `save` |
| the joining helper | `HlPeers.update_gossip_config/4` | `updateGossipConfig(...)` ¹ | `update_gossip_config` | `update_gossip_config` |
| default path | `default_config_path/0` | `defaultConfigPath()` ¹ | `default_config_path()` | `default_config_path()` |

¹ TypeScript's filesystem members live in the separate
`hl-peers/gossip-fs` entry point, so importing the core never
pulls in `node:fs` (`CLIENT_SPEC.md` §6.1). Elixir's `save/3` is on
`HlPeers.GossipConfig`; the behaviour is identical in both.

### Options — the same keys everywhere

| option | Elixir | TypeScript | Rust | Python |
|---|---|---|---|---|
| merge cap | `max_roots:` | `maxRoots` | `with_max_roots` | `max_roots=` |
| merge strategy | `mode:` | `mode` | `with_mode(MergeMode)` | `mode=` |
| pinned IPs | `keep_existing:` | `keepExisting` | `with_keep_existing` | `keep_existing=` |
| never include | `exclude:` | `exclude` | `with_exclude` | `exclude=` |
| 🔴 permission bits | `file_mode:` | `fileMode` | `with_file_mode` | `file_mode=` |
| pagination guard | `max_items:` | `maxItems` | `IterOpts::max_items` | `max_items=` |

🔴 **`file_mode`, never `mode`** (`CLIENT_SPEC.md` §6.6): `mode` is the merge
strategy and nothing else. The two would collide the moment
`update_gossip_config` takes one options bag.

### Errors

One closed taxonomy (`CLIENT_SPEC.md` §5.1), four mechanisms:

| taxonomy name | Elixir | TypeScript | Rust | Python |
|---|---|---|---|---|
| `RateLimited` | `%Error{kind: :rate_limited}` | `RateLimitedError` | `Error::RateLimited` | `RateLimited` |
| `FilterUnavailable` | `:filter_unavailable` | `FilterUnavailableError` | `Error::FilterUnavailable` | `FilterUnavailable` |
| `ChainNotServed` | `:chain_not_served` | `ChainNotServedError` | `Error::ChainNotServed` | `ChainNotServed` |
| `CursorExpired` | `:cursor_expired` | `CursorExpiredError` | `Error::CursorExpired` | `CursorExpired` |
| `BadRequest` | `:bad_request` | `BadRequestError` | `Error::BadRequest` | `BadRequest` |
| `Unauthorized` | `:unauthorized` | `UnauthorizedError` | `Error::Unauthorized` | `Unauthorized` |
| `NotFound` | `:not_found` | `NotFoundError` | `Error::NotFound` | `NotFound` |
| `Unavailable` | `:unavailable` | `UnavailableError` | `Error::Unavailable` | `Unavailable` |
| `Server` | `:server` | `ServerError` | `Error::Server` | `ServerError` |
| `Transport` | `:transport` | `TransportError` | `Error::Transport` | `TransportError` |
| `Decode` | `:decode` | `DecodeError` | `Error::Decode` | `DecodeError` |
| `Invalid` | `:invalid` | `InvalidError` | `Error::Invalid` | `Invalid` |
| `Truncated` | `:truncated` | `TruncatedError` | `Error::Truncated` | `Truncated` |

🪤 Python and TypeScript spell the `Server` variant `ServerError` because `Server`
alone would read as a server object; the taxonomy name in fixtures and in
`tools/cross_check` is still `Server`.

The six warning codes are literal strings and are **identical in all four**:
`many_roots_gt_24`, `many_roots_gt_64`, `chain_absent`,
`try_new_peers_false_multi_root`, `truncated`, `exclude_overrides_keep_existing`.

---

## What each language directory carries

1. `docs/<lang>.md` — install, a 10-line quickstart against the mock server, the
   full API surface, the error type, and how to run the conformance runner.
2. `<lang>/README.md` — the package README that ships with the package. It must
   carry the four gossip-config safety notes from the root README; a user who
   installs the package from a registry and never sees this repo must still be
   told that an empty root list crash-loops their node and that writing the file
   does not steer a running one.

Every runnable example in either file must work against
`python3 tools/mock_server.py`, with no live host and no API key. How that is
enforced differs by language: Python runs its documented blocks against the mock
server, Rust runs its README as doctests, and TypeScript resolves every documented
name against the real module.
