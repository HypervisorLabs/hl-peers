# The Python client — fuller guide

Package `hl-peers` (version 1.1.0), import
`hl_peers`. Python ≥ 3.9, no required runtime dependencies, fully typed
(`py.typed` ships in the wheel). It talks to `https://api.hypervisor.gg` by
default, on plain paths at the host root.

Normative behaviour lives in [`../CLIENT_SPEC.md`](../CLIENT_SPEC.md); this page
is how that behaviour looks in Python and why some of it is shaped the way it
is. Start with [`../python/README.md`](../python/README.md) for the quickstart —
this guide assumes you read it.

---

## Layout

| module | what is in it | imports |
|---|---|---|
| `hl_peers.gossip` | `GossipConfig`, `ConfigWarning`, `SaveResult`, `render_config`, `default_config_path` | **stdlib only** |
| `hl_peers.query` | `PeerQuery` and the presets (`tier1`, `sentries`, `not_full`, `mempool_servers`) | stdlib |
| `hl_peers.models` | `Peer`, `PeerMempool`, `PeerAuction`, `PeerPage`, `PeerDetail`, `Facets`, `RateLimit`, `Envelope`, `NotModified`, `GossipConfigResult`, `NetworkMapGraph`, `NetworkMapSummary` | stdlib |
| `hl_peers.errors` | the exception hierarchy, `ERROR_KINDS` | stdlib |
| `hl_peers.transport` | `Transport` protocol, `UrllibTransport`, `HttpResponse` | stdlib |
| `hl_peers.client` | `PeerApiClient`, `PeerIterator` | stdlib |
| `hl_peers.update` | `update_gossip_config` | stdlib |
| `hl_peers.cli` | `hl-peers` | stdlib (`argparse`) |
| `hl_peers.aio` | `AsyncPeerApiClient` | **needs `[httpx]`** |
| `hl_peers.httpx_transport` | `HttpxTransport` (sync) | **needs `[httpx]`** |

`AsyncPeerApiClient` and `HttpxTransport` are re-exported lazily from the
package root through `__getattr__`, so importing `hl_peers` on a box without
httpx costs nothing and fails nothing.

`GossipConfig` never imports the HTTP layer. That is a design rule, not an
accident: an operator must be able to edit `override_gossip_config.json` on a
box with no API access at all.

---

## Everything on a result

No response detail is hidden. Every successful result carries the decoded body,
the envelope, `rate_limit`, `etag`, `from_cache`, the HTTP `status` and
`api_version` (the `API-Version` header — informational, never validated):

```python
# runnable: mock
from hl_peers import PeerApiClient

page = PeerApiClient().list_peers()
print(page.status, page.from_cache, bool(page.etag), page.api_version)
print(page.envelope.version, page.available, page.stale, page.chain)
print(page.generated_at, page.next_sweep_at, page.attribution)
print(page.count, page.total, page.next_cursor)
print(page.rate_limit.limit, page.rate_limit.remaining, page.rate_limit.reset)
print(page.rate_limit.policy)
```

`PeerPage` is iterable and sized, and `ips()` is there because a root list is
the most common thing you want out of it.

A key the server adds later is never dropped: unknown top-level keys land in
`envelope.extra` and unknown peer keys in `peer.extra`. `mempool` and `auction`
are always present on a `Peer`; a server that omits them yields the all-`None`
objects, which mean **unknown**.

```pycon
>>> from hl_peers.models import Peer
>>> peer = Peer.from_json({"id": "192.0.2.10", "tier": None, "brand_new": 7})
>>> peer.tier is None            # not placed — NOT tier 0
True
>>> peer.extra["brand_new"]
7
>>> peer.labels                  # an open set; unknown labels render plain
[]
>>> peer.mempool.serves is None  # unknown, NOT "does not serve"
True
>>> peer.auction.status is None  # never seen in the auction
True

```

The model is hand-shaped, not generated. `tests/test_v1_additions.py` reads
`spec/peer-api-v1.fields.json` (generated from the vendored public OpenAPI) at test
time and asserts that `Peer`, `PeerMempool`, `PeerAuction`, `Envelope`, the page,
facets and network-map types cover exactly those field sets — so a field added
upstream fails the suite until the model follows, without a code generator in
the loop.

---

## The query builder

Immutable, chainable, one method per wire parameter. CSV parameters take a list
— or several arguments, or a pre-joined string:

```pycon
>>> from hl_peers import PeerQuery
>>> PeerQuery().cc("de", "nl").tier(1, None).sort("random").to_query_string()
'cc=DE%2CNL&sort=random&tier=1%2Cnull'

```

Three things to notice: `cc` is upper-cased, `tier`'s `None` member serializes
as the string `null` (it selects *unplaced* nodes), and the parameters come out
**lexicographically ordered with the comma percent-encoded**, so two clients
issuing the same query produce the same URL — and the same CDN cache key.

`serves_mempool` and `auction` work the same way — `None` is a selectable
member, the literal `null`:

```pycon
>>> PeerQuery().serves_mempool(True, None).auction("bid_before", None).to_params()
{'auction': 'bid_before,null', 'serves_mempool': 'true,null'}
>>> PeerQuery().country("jp").state("responsive", "open").to_query_string()
'country=JP&state=responsive%2Copen'

```

### What is validated locally, and what is not

Refused before a request is spent: `seen_within` not matching `^[0-9]+[mhd]$`;
`min_uptime` outside `[0, 1]`; negative `max_tier` / `max_blocks_behind` /
`max_latency_ms`; `limit < 1`; `max_latency_ms` without `vantage`; and a value
outside a **closed** enum (`chain`, `network`, `sort`, `format`, `connectable`,
`state`, `serves_mempool`, `auction`) — case-sensitive.

Deliberately **not** validated: `cc`, `region`, `asn`, `role`, `label`,
`version`, `fields`. Those are open sets that may grow additively within v1, and
validating them here would turn a server `400` that names the problem into a
client-side error that guesses at it. `limit` is not clamped either: the server
clamps by tier, and clamping locally would hide that ceiling from you.

No `sort` is sent unless you set one (D-J), so the server's default governs.

### `network` (testnet, beta)

`network` is lower-case and closed (`"mainnet"` | `"testnet"`), so it can never
be confused with the capitalised `chain` enum. Unset sends no key at all — a
mainnet URL is exactly what it was before, and hits the same cache key:

```pycon
>>> PeerQuery().cc("DE").network("testnet").to_query_string()
'cc=DE&network=testnet'
>>> PeerQuery().network("Testnet")
Traceback (most recent call last):
  ...
hl_peers.errors.Invalid: ...

```

The same value is a `network=` keyword on `get_peer` and the network map, and a
client-wide default (`PeerApiClient(network="testnet")`) that applies to every
call setting none; a per-call or per-query value wins. On a testnet body every
result's `network` is `"testnet"` and each `Peer.version` is the node's hardfork
version (opaque string, `None` when not reported); on mainnet both are `None`.
`400 network-not-served` is a plain `BadRequest` with `parameter == "network"`.
See the README's *Testnet (beta)* section for the known gaps.

---

## Pagination

`iter_peers` returns a `PeerIterator`: a lazy generator with a handle you can
inspect afterwards.

```python
# runnable: mock
from hl_peers import PeerApiClient, PeerQuery

client = PeerApiClient()
iterator = client.iter_peers(PeerQuery().limit(2).sort("tier"), max_items=3)
ips = [peer.id for peer in iterator]
assert iterator.truncated is True          # the guard fired
assert iterator.pages == 2
assert iterator.page is not None and iterator.page.available is True
print(ips)
```

It stops on a null `next_cursor`, an empty page, `available == false`, or
`max_items` (default **10 000**, never unbounded). On `available == false` it
stops **without raising** — inspect `iterator.page.available` to tell that apart
from "nothing matched", because they are different answers.

It never deduplicates across pages and never reorders: the API already ranked
the list, and re-ranking it would quietly answer a question you did not ask.

`list_all_peers()` is the eager convenience with the same guard.

---

## Conditional requests

The cache is on by default: 64 entries, LRU, in memory, per client instance,
never on disk. The key is
`method \0 path \0 canonical_query \0 (k|a)` — the last field records **whether a
key was sent**, because the tier changes the page ceiling and therefore the
body.

* On a hit the client sends `If-None-Match` and a `304` returns the stored body
  with `from_cache=True` and the rate-limit headers **from the 304**.
* `Cache-Control: max-age` is not honoured as a hard TTL. The client always
  revalidates: the cache exists to make polling free, not to serve stale bytes.
* Supply `if_none_match=` yourself and you get a `NotModified` result instead of
  a body — you asked the conditional question, so you get the conditional
  answer.

```python
# runnable: mock
from hl_peers import PeerApiClient
from hl_peers.models import NotModified

client = PeerApiClient()
first = client.list_peers()
assert client.list_peers().from_cache is True          # a 304, no quota unit spent
assert isinstance(client.list_peers(if_none_match=first.etag), NotModified)
```

Turn it off with `PeerApiClient(cache=None)`… which is not a thing you want:
pass `--no-cache` on the CLI only when you are debugging the server.

---

## Errors and retries

`ERROR_KINDS` maps the CLIENT_SPEC §5.1 variant names to the exception classes,
which is how the conformance runner stays language-neutral:

```pycon
>>> from hl_peers.errors import ERROR_KINDS
>>> ERROR_KINDS["FilterUnavailable"].__mro__[1].__name__   # a 400-family sibling
'BadRequest'
>>> sorted(ERROR_KINDS)[:4]
['BadRequest', 'ChainNotServed', 'CursorExpired', 'Decode']

```

Discrimination is on the last path segment of `problem.type`, never on `title`
or `detail` — prose is not an API. A non-problem+json error body still maps on
status alone (D-K), with `problem = None` and a ≤ 512-byte body excerpt
truncated on a UTF-8 boundary.

**Retries are off by default** (`max_retries=0`, D-Q): a `429` raises
`RateLimited` at once, with `reset_at` — an aware UTC `datetime`, computed from
the injectable clock as `now + retry_after`, else `now + reset`, else `None` —
and a `5xx` or transport failure raises at once. The library never sleeps
behind your back unless you pass `max_retries=N`.

```pycon
>>> from datetime import datetime, timezone
>>> from hl_peers.models import compute_reset_at
>>> compute_reset_at(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc), 42, None)
datetime.datetime(2026, 10, 1, 12, 0, 42, tzinfo=datetime.timezone.utc)

```

**Retry decisions once enabled**, all testable without a wall clock: `429`
(sleeping `Retry-After`), `5xx` including `503`, and transport failures. Never
any other 4xx. A `Retry-After` above `max_retry_after` (default 60 s) raises immediately
rather than blocking your thread for minutes (D-I).

`Retry-After` may be a delay in seconds **or** an RFC 9110 IMF-fixdate. Digits
are used as-is; a date is resolved against the client's **injectable clock**
(`PeerApiClient(clock=…)`, the same one that stamps a `.bak`) as
`max(0, date - now)`, so a past date yields `0` and a value that parses as
neither is treated as absent and falls back to backoff. The resolved seconds are
what `rate_limit.retry_after` exposes; the header as sent stays in
`rate_limit.retry_after_raw`.

```pycon
>>> from datetime import datetime, timezone
>>> from hl_peers.models import parse_retry_after
>>> parse_retry_after("Mon, 21 Sep 2026 02:36:00 GMT",
...                   datetime(2026, 9, 21, 2, 35, tzinfo=timezone.utc))
60
>>> parse_retry_after("soon-ish") is None
True

```

Backoff is `min(500ms * 2^n, 30s) * (0.5 + random()/2)`. Both `sleep` and
`random` are injectable, which is how the suite asserts on *decisions* and never
on wall-clock delays:

```python
# runnable: mock
from hl_peers import PeerApiClient

sleeps = []
client = PeerApiClient(sleep=sleeps.append, random=lambda: 0.5, max_retries=2)
client.list_peers()          # nothing to retry here
assert sleeps == []
```

Anonymous is a supported tier: omitting a key is never a `401`. The key is sent
as `Authorization: Bearer …` and never logged, never in a query string, never in
an error message.

---

## Facets and the network map

See the network map: <https://hypervisor.gg/network> — the live graph these endpoints serve.

`facets(query)` takes every filter of the builder and refuses the six non-filters
(`sort`, `limit`, `cursor`, `fields`, `format`, `try_new_peers`) with `Invalid`.
Facet names are an open map; `serves_mempool`, `auction` and `uptime` are there
today, and `geo_coverage` is exposed as a plain mapping.

`network_map_graph(**opts)` and `network_map_summary(**opts)` take keyword
options (`max_tier`, `show_unreached`, `hide_unreachable`, `connectable`, `cc`,
`operator`, `label`, `serves_mempool`, `auction` and `network`, with the
builder's closed enums), serialize them like `/peers` (lexicographic, `%2C`) —
except that the string filters are sent exactly as given, with no upper-casing — and
share the ETag cache, error mapping and `api_version`/`rate_limit` exposure.
Graph `nodes` are decoded by the `Peer` decoder (so each carries `city`); an
edge's source is `from_` because `from` is a keyword. The graph and
the summary carry `cities` (a list of `CityCluster`, or `None` when no city
positions are served) and `attribution`; the summary adds `serves_mempool` and
`auction` bucket counts.

```python
# runnable: mock
from hl_peers import PeerApiClient

client = PeerApiClient()
graph = client.network_map_graph(max_tier=2)
tiers = {t.tier: t.count for t in graph.tiers}
print(tiers, graph.operator_nodes.count, graph.truncated)
```

```pycon
>>> from hl_peers._http import network_map_query_string
>>> network_map_query_string(max_tier=2, connectable=["yes", "no"], cc="de")
'cc=de&connectable=yes%2Cno&max_tier=2'
>>> network_map_query_string(serves_mempool=[True, None], auction="holds_slot")
'auction=holds_slot&serves_mempool=true%2Cnull'

```

---

## GossipConfig

### Serialization

`to_json()` is byte-exact across all four language clients, which is what makes
the 76 conformance cases meaningful. The rules: fixed key order
(`chain`, `root_node_ips`, `try_new_peers`, `reserved_peer_ips`,
`n_gossip_peers`, `split_client_blocks`), unknown keys after them sorted by code
point, 2-space indent, `": "` after a colon, empty array as `[]` on one line,
literal UTF-8, exactly one trailing `\n`.

```pycon
>>> from hl_peers import render_config
>>> print(render_config({"try_new_peers": True, "root_node_ips": [],
...                      "zz": {"b": 1, "a": 2}, "note": "Köln"}), end="")
{
  "root_node_ips": [],
  "try_new_peers": true,
  "note": "Köln",
  "zz": {
    "a": 2,
    "b": 1
  }
}

```

🪤 A root entry renders **expanded** across three lines, not as the one-line
`{ "Ip": "…" }` the upstream prose examples show. Whitespace is not significant
to the node's parser; what matters is that four languages agree, and a uniform
rule is easier to agree on than an exception.

### validate()

Returns warnings, or raises the **first** error in a fixed order so all four
languages report the same one: empty/missing `root_node_ips` → entry shape →
IPv4 validity → duplicates → chain spelling → `n_gossip_peers` range → boolean
types.

```pycon
>>> from hl_peers import GossipConfig, Invalid
>>> cfg = GossipConfig.parse('{"root_node_ips": [{"Ip": "192.0.2.10"}], "try_new_peers": true}')
>>> [w.code for w in cfg.validate()]
['chain_absent']
>>> try:
...     GossipConfig.parse('{"root_node_ips": [{"ip": "192.0.2.10"}]}').validate()
... except Invalid as error:
...     print(error.parameter)
root_node_ips[0]

```

The capitalization is load-bearing: `ip` is not `Ip`. A bare string inside a
*parsed* `root_node_ips`, a lowercase `ip`, and an extra `port` key are all
refused — bare strings are accepted only as constructor **input**.

IPv6 is refused everywhere (D-B), with the reason spelled out: the peer API is
IPv4-only and the node's support for an IPv6 literal here is *unverified*.
Refusing is the safe side of an unknown
whose failure mode is a crash loop.

`set_n_gossip_peers` refuses outside `[8, 100]` at both ends (D-C) instead of
clamping: silently clamping would write a number you did not choose into a file
you will read back later.

`set_split_client_blocks(False)` **removes** the key, because the node's own
serializer omits it when false — but an explicit `false` already in a parsed
file survives a round trip. Only the setter removes it.

### merge_peers

```pycon
>>> from hl_peers import GossipConfig
>>> cfg = GossipConfig.new("Mainnet", ["192.0.2.10"])
>>> cfg.merge_peers(["203.0.113.1", "203.0.113.2"], mode="append",
...                 keep_existing=["198.51.100.7"])   # returns ITS warnings
[]
>>> cfg.root_ips()
['198.51.100.7', '192.0.2.10', '203.0.113.1', '203.0.113.2']
>>> [w.code for w in cfg.pending_warnings]            # …and retains them
[]

```

`merge_peers` mutates in place and **returns its warnings**, which it also
retains on the config as `pending_warnings`; `validate()`, `save()` and
`update_gossip_config()` report the union with §6.4's state-derived warnings,
de-duplicated by code, so a warning cannot be lost by a caller who ignored the
return value. `clear_warnings()` is the only thing that drops them — a
successful `save` does not. `from_peers` is a constructor and returns the config,
with its warnings pending.

Order is `keep_existing ++ (candidates | existing, per mode)`, then `exclude`
(which beats `keep_existing`, with a warning naming the IP), then dedupe
(first wins), then truncate to `max_roots` keeping the **head**. An invalid
candidate is an error, never a silent drop — silently dropping it would shrink
your root list without telling you. An empty result raises.

### From the server: list_peers_as_gossip_config

The API renders the override file itself for `format=gossip_config`. The helper
returns a `GossipConfigResult` — `config`, `rate_limit`, `etag`, `from_cache`,
`api_version`, `status` — and never writes; `result.config.save(path)` (or the
identical shortcut `result.save(path)`) does. The rendered bytes are the §6.3
canonical form, not the server's key order; the golden test pins
`to_json()` and the saved bytes to
`fixtures/http/peers_format_gossip_config.json`'s
`expect.gossip_config_json_text`.

### save

`validate()` first, so an invalid config never reaches the filesystem. Then:
identical bytes → no-op; `dry_run` → unified diff and nothing written; optional
`.bak` with a UTC stamp (the only clock this module reads, and it is
injectable); then temp file in the **same directory**, fsync, `os.replace`,
fsync the directory. Permissions are preserved, never widened, and `0600` for a
new file unless `file_mode=` says otherwise.

🪤 The permission option is **`file_mode`** (`save`, `update_gossip_config`,
`--file-mode`). `mode` is the merge strategy of §6.5 and nothing else — the two
would collide the moment `update_gossip_config` takes one options bag.

```python
# runnable: mock
import os
from hl_peers import GossipConfig

path = os.path.expanduser("~/override_gossip_config.json")
config = GossipConfig.new("Mainnet", ["192.0.2.10", "198.51.100.7"])
config.set_try_new_peers(True)
first = config.save(path)
second = config.save(path)
assert first.written and not second.written and not second.changed
print(oct(os.stat(path).st_mode & 0o777))
```

---

## Testing this package

```bash
cd python
uv venv .venv && . .venv/bin/activate && uv pip install -e ".[dev]"
pytest -q
```

* `tests/test_gossip_cases.py` — **one** generic runner over all **76** cases in
  `fixtures/gossip/cases`, byte-exact, with `expect.warnings` asserted by
  calling `validate()` after the last operation.
* `tests/test_http_behaviour.py` — every `fixtures/http` case through an
  injected transport. No socket.
* `tests/test_v1_additions.py` — mempool/auction
  fixtures, network map, `API-Version`, retries off by default, `reset_at`
  from an injected clock, the gossip-config golden test, the field-set check
  against `spec/peer-api-v1.fields.json`, and async parity.
* `tests/test_v1_1_additions.py` — the `listening`
  state, `Peer.city`, `cities` / `attribution` on the graph and summary, the
  summary's mempool/auction counts, the map's `serves_mempool` / `auction`
  filters and their local `Invalid`, and the flood `429`.
* `tests/test_integration_mock.py` — spawns `tools/mock_server.py` on a free
  port: stdlib client, httpx sync transport, httpx async client, and the
  `--fail-first-n` retry path.
* `tests/test_cli.py` — `hl-peers` as a subprocess against that server.
* `tests/test_save_fs.py` — atomicity, mode, `.bak`, dry-run, the zero-roots
  refusal leaving the file untouched.
* `tests/test_docs.py` — every `# runnable: mock` block in the README and in
  this page is executed, and every `pycon` block runs as a doctest.

No network is required, reachable or consulted in the default run.

---

## Troubleshooting

**Pointing at something other than the public host** — pass `base_url=` or set
`HL_PEER_API_BASE_URL` (for example to `tools/mock_server.py` in tests). If a
proxy mounts the API under a prefix, pass `base_path=`.

**`RateLimited` straight away** — expected: retries are off by default. Wait
until `error.reset_at`, get a key (`error.signup_url`), or opt in with
`max_retries=3`.

**`FilterUnavailable` on `region`, `asn` or `serves_mempool`** — that attribute
is not loaded right now. Call `facets()` and check `facet_available(...)`
before filtering; `cc` is the broad geo key.

**`BadRequest` with `parameter == "mempool"`** — the server does not offer a
`mempool=` filter (`filter-removed`); use `serves_mempool(True)` / `mempool_servers()`.

**`Unavailable` from `update_gossip_config`** — the producer could not build a
graph (`available: false`). Nothing was written, on purpose: turning that into
an empty peer set would empty your root list, which is the crash-loop shape
(D-G).

**`Invalid: root_node_ips: refusing to emit an empty root_node_ips…`** — your
filter matched nothing. Widen it, or pass `keep_existing=[…]` with IPs you want
pinned regardless. Do not work around it.

**The node did not pick up the new file** — it would not. The node reads
`override_gossip_config.json` when it starts. Nothing in this
library restarts anything; the restart is yours to schedule.
