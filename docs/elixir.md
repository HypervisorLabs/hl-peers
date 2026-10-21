# The Elixir client — guide

`hl_peers` is the Elixir member of this monorepo's four clients. It does two
separate jobs and keeps them separate:

| namespace | nature | needs network? |
|---|---|---|
| `HlPeers`, `HlPeers.Client`, `HlPeers.Query` | the HTTP query surface | yes |
| `HlPeers.GossipConfig` | pure, offline, deterministic file algebra | **never** |

`elixir/README.md` is the quickstart and the recipe book. This page is the
fuller guide: the shape of the API, the decisions behind it, and the places
where the shared spec and fixtures needed a reading.

Version **1.1.0**. The default host is `https://api.hypervisor.gg`, with plain
paths at the host root. Install it from git (`{:hl_peers, github:
"HypervisorLabs/hl-peers", sparse: "elixir"}`) or as a path dependency; a Hex
package will follow.

---

## API surface

### Configuration

`HlPeers.Client.new/1` — `:base_url` (`https://api.hypervisor.gg`),
`:base_path` (`""`), `:api_key`, `:timeout`, `:user_agent`
(`hl-peers-elixir/1.1.0`), `:user_agent_suffix`, `:max_retries` (**0**),
`:max_retry_after`, `:min_poll_interval`, `:cache`, `:adapter`,
`:adapter_opts`, and the test seams `:sleep_fun` / `:rand_fun` / `:clock_fun`.
`:suppress_placeholder_warning` is accepted and ignored.

Resolution happens once, at construction: `base_url` is explicit →
`HL_PEER_API_BASE_URL` → `https://api.hypervisor.gg`, `api_key` is explicit →
`HL_PEER_API_KEY` → anonymous. Anonymous is a **supported tier, not an error**.
`base_path` is explicit only (no environment variable) and exists for a proxy
that mounts the API under a prefix; `Client.default_base_path/0` is the one
place the empty default is spelled.

🔴 Auto-retry is **off** by default (D-Q): a `429` returns `:rate_limited`
with `reset_at` (the UTC instant to sleep until, from `:clock_fun`), and a
`5xx` / transport failure returns at once. `max_retries: n` opts in to §5.2's
unchanged policy.

The client carries one process: a small `Agent` (`HlPeers.Store`) holding the
ETag cache. It is linked to whoever
called `new/1`, is per instance, and is never global and never on disk. Nothing
else in the library needs a process — `HlPeers.GossipConfig` never touches it.

### Queries

`HlPeers.Query` is immutable and chainable, with one setter per wire
parameter (`Query.cc/2`, `Query.tier/2`, `Query.seen_within/2`, …) plus
`Query.new/1`, `Query.put/3` and `Query.merge/2`. CSV parameters take a list and
are joined at serialization; a pre-joined string is accepted too.

Serialization is canonical: parameters lexicographic by name, values
percent-encoded per RFC 3986 with the CSV comma encoded as `%2C` (no special
case), unset values and empty lists omitted entirely. Two clients issuing the
same query produce the same URL, and so the same CDN cache key.

Local validation is **deliberately thin** — `seen_within`'s shape,
`min_uptime`'s range, non-negative ceilings, `limit >= 1`, `max_latency_ms`
requiring a `vantage`, and closed enums (`chain`, `sort`, `format`,
`connectable`, `state` incl. `responsive` / `open`, `serves_mempool` over
`true` / `false` / `nil`, `auction` over `holds_slot` / `bidding` /
`bid_before` / `nil`). `cc`, `country`, `region`, `asn`, `role`, `label`,
`version` and `fields` are open sets and are sent as given (`cc` / `country`
upper-cased); `limit` is not clamped. No `sort` is sent unless you set one
(D-J). `try_new_peers` is only meaningful with `list_peers_as_gossip_config/3`.

### Calls

| function | answers |
|---|---|
| `list_peers/3` | `{:ok, %PeerList{}}` \| `{:ok, %NotModified{}}` \| `{:error, %Error{}}` |
| `stream_peers/3` (alias `iter_peers/3`) | a lazy `Stream` of `%Peer{}` |
| `list_all_peers/3` | `{:ok, [%Peer{}]}` — the eager convenience, same guard |
| `get_peer/3` | `{:ok, %PeerEnvelope{}}` |
| `facets/3` | `{:ok, %Facets{}}` — every filter accepted; `sort`/`limit`/`cursor`/`fields`/`format`/`try_new_peers` refused |
| `list_peers_as_gossip_config/3` | `{:ok, %GossipConfigResult{config: %GossipConfig{}}}` — never writes |
| `network_map_graph/3` | `{:ok, %NetworkMap.Graph{}}` |
| `network_map_summary/3` | `{:ok, %NetworkMap.Summary{}}` |
| `tier1/2`, `sentries/2`, `not_full/2`, `mempool_servers/2` | the owner's presets |
| `update_gossip_config/4` | `{:ok, %UpdateResult{}}` |

See the network map: <https://hypervisor.gg/network> — the live peer graph these endpoints serve.

Each has a `!` variant that raises `%HlPeers.Error{}` (it is an exception
struct). `Query.tier1/0`, `Query.sentries/0`, `Query.not_full/1` and
`Query.mempool_servers/0` return the preset **queries**, for refining before
sending — owner query 1 is `Query.mempool_servers() |> Query.cc(["DE"])`, and
it excludes unknown (`serves: nil`) peers, because "unknown" is not
"does not serve".

The network-map calls take a keyword list of filters (`:max_tier`,
`:show_unreached`, `:hide_unreachable`, `:connectable`, `:cc`, `:operator`,
`:label`, `:serves_mempool`, `:auction`), validated by
`HlPeers.NetworkMap.to_params/1`; an unknown filter is a local `Invalid`.
`:serves_mempool` and `:auction` go through `HlPeers.Query`'s own validation
and serialization, so they behave exactly as on the peers query. The graph and
summary carry `cities` (`%HlPeers.NetworkMap.CityCluster{}`) and
`attribution`, both `nil` when no city positions are served; the summary also
carries `serves_mempool` / `auction` count maps.

### Results

Nothing about a response is hidden: every result carries `:envelope`,
`:rate_limit`, `:api_version` (the `API-Version` header — informational, never
a failure), `:etag`, `:from_cache`, `:status` and the decoded `:body`. Every
`%Error{}` carries `:rate_limit` and `:api_version` too. The envelope includes
`attribution`, the geo-data licence line.

`%Peer{}` decodes every schema field plus an `:extra` map for anything the
server adds later. `tier: nil` stays `nil` (that node was never placed in the peer graph — it
is **not** tier 0); `connectable` stays a three-state string and is never
folded — the only boolean offered is `Peer.connectable?/1`, named for the
positive confirmation only. The geo fields `cc_confidence`,
`cc_source`, `asn`, `org` are open strings; `uptime` is a nullable fraction and
`feeders` a list of IPv4 strings. `mempool` is always a
`%Peer.Mempool{serves, last_checked, verdict, first_frame_ms, source}` and
`auction` a `%Peer.Auction{status, slot, since, last_bid_at}`: an all-`nil`
struct means unknown, `serves: nil` is never `false`, and a peer from an older
server that omits the key decodes to the all-`nil` struct.
`test/v1_additions_test.exs` asserts that `Peer`, `Peer.Mempool`,
`Peer.Auction`, `Envelope`, the query parameters and the network-map results
cover **exactly** the field sets in `spec/peer-api-v1.fields.json`, read at
test time.

`%Facets{}` keeps "not collected" (`available: false`) and "collected, nothing
matched" (`available: true, values: []`) distinguishable:
`Facets.available?/2` answers the first, `Facets.values/2` returns
`{:ok, values}` or `:unavailable` rather than a bare list. Facet names are an
open map (now including `serves_mempool` and `auction`), and `geo_coverage` is
kept as the server's raw map.

### Gossip config

`HlPeers.GossipConfig` implements every method in `CLIENT_SPEC.md` §6:
`parse/1`, `load/1`, `load_or_new/2`, `new/2`, `to_json/1`,
`to_json_validated/1`, `default_config_path/0`, the root and reserved-peer
algebra, `set_chain/2`, `set_try_new_peers/2`, `set_n_gossip_peers/2`,
`set_split_client_blocks/2`, `unset_split_client_blocks/1`, `validate/1`,
`merge_peers/3`, `from_peers/2` and `save/3`.

Return convention: mutators that cannot fail return the config so they chain;
the ones that can answer `{:ok, …}` / `{:error, %Error{}}`, and `merge_peers/3`
and `from_peers/2` answer `{:ok, config, warnings}`.

The serializer (`HlPeers.GossipConfig.Serializer`) is written out longhand
rather than delegated to `Jason`, whose encoder gives neither the fixed key
order nor the separators the file needs. It matches Python's
`json.dumps(obj, indent=2, ensure_ascii=False, separators=(",", ": ")) + "\n"`
under the spec's key ordering, which is the reference implementation in
`tools/gen_fixtures.py`.

---

## Testing

```bash
cd elixir
mix test                        # offline — no network, no Python, no port
mix test --include integration  # also boots tools/mock_server.py on a free port
```

* **`test/gossip_conformance_test.exs`** — *one* generic runner over all **76**
  `fixtures/gossip/cases/*.json`, in the format of `fixtures/README.md`,
  including `$TMPDIR` substitution, `file_after` / `file_absent`, byte-exact
  `json_text`, and `expect.warnings` compared as the **set** `validate/1` returns
  after the last operation.
* **`test/http_test.exs`** and **`test/v1_additions_test.exs`** — all **35**
  HTTP fixtures on plain paths: every endpoint (incl. both `/network-map/*`),
  the four owner queries (query 1 whole: `serves_mempool=true` AND `cc=DE`),
  `serves_mempool=null`, `auction=holds_slot,bidding`, the 3-page cursor walk,
  `304` + the caller-supplied `If-None-Match` case, `400 filter-unavailable`
  (on `asn`, `region` and `serves_mempool`), `filter-removed`,
  `chain-not-served`, `cursor-expired`, `401`, `404`, `500` with a non-problem
  body, `503`, the four `429`s, `available: false`, `stale: true`,
  `API-Version` on results and errors, facets, and the §4.7 **golden**
  gossip-config test (`to_json/1` and the bytes `save/3` writes both equal
  `expect.gossip_config_json_text`).
* **`test/retry_test.exs`** — asserts on the *decisions* (which statuses retry,
  how many attempts, that `Retry-After` was read) and never on wall-clock
  delays; the sleep function is injected. Retries off by default (one `503` ⇒
  one request), explicit `max_retries` still working, and `reset_at` from an
  injected clock.
* **`test/gossip_config_fs_test.exs`** — atomicity (temp file in the same
  directory, none left behind), mode preservation, `0600` for a new file, the
  timestamped `.bak`, `dry_run` writing nothing, and the zero-roots refusal
  leaving the target untouched.
* **`test/doc_test.exs`** and **`test/readme_examples_test.exs`** — every
  documented example is executed.

Quality gates: `mix format --check-formatted`, `mix compile
--warnings-as-errors`, `mix credo --strict`, `mix dialyzer`, `mix docs`.

---

## Spec points this client implements deliberately

Where `CLIENT_SPEC.md` leaves a choice to the language, this client reads it
as follows.

### Language-level readings

* **Timestamps inside `mempool` / `auction`** are parsed to UTC `DateTime`
  (§3.2: "otherwise parse them" — this client already uses `DateTime` for its
  clock and `reset_at`). A value that is not an ISO-8601 instant decodes to
  `nil` rather than failing the page. The envelope's and `Peer.last_seen`'s
  timestamps stay strings.
* **`api_version` on a cache-served `304`** comes from the `304` response,
  exactly like its rate-limit block (§4.5), so it is `nil` when the `304`
  omits the header.
* **`list_peers_as_gossip_config/3`** returns `%GossipConfigResult{}`; a
  caller-supplied `If-None-Match` still answers `%NotModified{}`. There is no
  `path:` sugar — `GossipConfig.save/3` is the one writer.
* **Network-map `cc` / `operator` / `label`** accept a string or a list (a list
  is CSV-joined) and are sent as given — `cc` is **not** upper-cased there,
  because §4.8 types them as plain strings.

### Warning codes are independent (§6.4)

Every condition is evaluated on its own and every one that holds fires. 25 roots
with `try_new_peers: false` reports **both** `many_roots_gt_24` **and**
`try_new_peers_false_multi_root` — a many-roots warning does not suppress,
absorb or outrank the pinning warning, because they describe two unrelated facts.
The only mutually exclusive pair is `many_roots_gt_24` / `many_roots_gt_64`.
Pinned by `validate_warns_many_roots_and_pinning_together` and its escalated
twin.

### Merge warnings are pending on the config (§6.5)

`merge_peers/3` and `from_peers/2` return `truncated` /
`exclude_overrides_keep_existing` **and** retain them on the config value as
pending warnings. `validate/1`, `save/3` and `update_gossip_config/4` report the
union of the pending set and §6.4's state-derived set, de-duplicated by code, so
a warning raised during a merge cannot be lost by a caller who ignored what the
merge returned. The next merge **replaces** the pending set;
`GossipConfig.clear_warnings/1` is the only thing that drops it — a successful
`save` does not. Pinned by `merge_peers_truncation_warning_survives_to_save`.

### `file_mode`, never `mode` (§6.6)

`save/3` and `update_gossip_config/4` take `:file_mode` for permission bits;
`:mode` is `merge_peers/3`'s strategy and nothing else.

### `Retry-After` may be an HTTP-date (§5.2)

Digits are seconds; otherwise the value is parsed as an RFC 9110 IMF-fixdate and
converted with `max(0, date - now)` against the client's injectable clock
(`Client.new(clock_fun: fn -> ... end)`), so a test never depends on wall time. A
past date yields `0`; a value that parses as neither is **absent** and the
backoff takes over — the request never fails over a bad header. The computed
seconds are what `rate_limit.retry_after` exposes, and the raw header stays in
`rate_limit.retry_after_raw`. The cap still applies, so an HTTP-date an hour out
raises `RateLimited` rather than blocking (D-I). Pinned by
`error_429_retry_after_http_date` (with its injected `clock.now`) and
`error_429_retry_after_unparseable`; the retry test walks **every** `429`
fixture and asserts its `expect.retry_after_seconds`.

### `load`/`save` stay on the config value

§6.1 permits a separate filesystem module (`GossipConfig.FS`). Elixir has no
filesystem-free target here, so `load/1` and `save/3` remain functions on
`HlPeers.GossipConfig`; the behaviour is identical either way.

### The module namespace is `HlPeers`

The package is `:hl_peers` and every module lives under `HlPeers`.

---

## Deliberately not built

* **The CLI** (`CLIENT_SPEC.md` §8 makes it optional; the root README's matrix
  records Elixir as `—`).
  `mix hl_peers` is not implemented; the library surface covers every flag's behaviour, and the
  "render to stdout and place the file yourself" path is
  `IO.write(result.json)`.
* **Directory `fsync` after the rename.** The file itself is `fsync`ed before
  the atomic rename, but the BEAM cannot open a directory handle, so the
  containing directory is not synced. §6.6 asks for it "where the platform
  supports it"; on the BEAM it does not, without a NIF this library will not
  take a dependency on.
* **The obsolete HTTP-date formats** (RFC 850, asctime). Only IMF-fixdate is
  parsed, per §5.2; anything else is absent and the backoff takes over.
