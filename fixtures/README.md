# The conformance suite

Language-neutral JSON. Every client in this monorepo is tested against these files,
so that four independent implementations converge without coordinating.

🔴 **Every IPv4 in here is an RFC 5737 documentation address** — `192.0.2.0/24`,
`198.51.100.0/24`, `203.0.113.0/24`. No real peer or fleet address may ever enter
this repo. `tools/check_fixtures.py` enforces this and will fail the build.

Regenerate with `python3 tools/gen_fixtures.py`; lint with
`python3 tools/check_fixtures.py`. Do not hand-edit a fixture — edit the generator,
which also carries the reference serializer.

---

## `http/` — recorded-style response fixtures

One file per case. The shape:

```jsonc
{
  "name": "peers_owner_q3_tier1",     // == the filename, minus .json
  "description": "why this case exists",
  "request":  { "method": "GET", "path": "/peers",
                "query":  { "tier": "1" },     // decoded values, not a query string
                "headers": { } },              // headers that must match, if any
  "response": { "status": 200,
                "headers": { "ETag": "\"sha256-…\"", "RateLimit-Limit": "20", … },
                "body": { … } }                // decoded JSON, or `null` on a 304
}
```

- `response.body_text` replaces `body` when the body is **not** JSON (the 500 case).
- `clock.now` and `expect` are optional top-level fields carrying what the *client*
  must compute from the response. `peers_format_gossip_config` carries
  `expect.gossip_config_json_text`: the **golden** bytes the client's
  `list_peers_as_gossip_config` helper must render (`to_json()`) and write
  (`save()`) for that response — the §6.3 canonical form, whose key order is not
  the server's. Every `429` carries
  `expect.retry_after_seconds` (`null` = treat as absent and back off), and a `429`
  whose `Retry-After` is an HTTP-date also carries `clock.now` — the instant to
  inject into the client's clock so the assertion never depends on wall time.
- 🔴 **Every non-null `next_cursor` is served by another fixture.** A dangling
  cursor is a dead end: an `iter_peers` walk would request a page the mock server
  does not have. The linter enforces this, so either null the cursor (single page)
  or add the follow-up page. The example-query fixtures are deliberately single-page;
  the cursor walk lives in `peers_page_{1,2,3}_of_3`.
- A fixture whose `request.headers` is non-empty matches only when every named
  header is present with that value, and it **outranks** a header-free fixture for
  the same method/path/query. That is how one query has both a 200 and a 429.
- `X-Mock-Scenario` is a mock-server affordance, not part of the API. Real clients
  never send it; fixture-driven tests do, to select an otherwise-unreachable
  response for the same query.

**Coverage.** 38 fixtures: all five endpoints (`/peers`, `/peers/{ip}`,
`/peers/facets`, `/network-map/graph`, `/network-map/summary`), plain paths at
the host root; each of the four example queries, including query 1 whole
(`serves_mempool=true` AND `cc=DE`); `serves_mempool=null`; `auction=holds_slot,bidding`;
`format=gossip_config` with its golden output; `available:false`; `stale:true`; a
3-page cursor walk; `304`; `400` × 7 (`filter-unavailable` on `asn`, `region` and
`serves_mempool`, `filter-removed`, invalid enum, `chain-not-served`,
`cursor-expired`, plus a malformed-IP 400 on the single-peer path); `401`; `429` × 5
(numeric `Retry-After`, an over-cap one, an HTTP-date one, an unparseable one, and the per-address flood one);
`503` problem+json; `500` with a non-problem body. Every response carries
`API-Version: 1`; every peer row carries the `mempool` and `auction` objects
(all-`null` when unknown) and `city`. Also: `peers_state_listening`,
`network_map_graph_mempool_auction` (the map's `serves_mempool` / `auction`
filters), `error_429_ip_flood` (the per-address flood limit's problem+json `429`);
the graph and summary carry `cities` + `attribution`, the summary its
`serves_mempool` / `auction` counts.

### Using them

Either stub your transport and replay the file, or run the mock server:

```bash
python3 tools/mock_server.py --port 8931 &
curl -s 'http://127.0.0.1:8931/peers?tier=1'
curl -si 'http://127.0.0.1:8931/peers' -H 'X-Mock-Scenario: rate-limited' | head -12
```

The server also synthesises a `304` for **any** fixture with an `ETag` when you
send a matching `If-None-Match`, and `--fail-first-n N` makes the first N requests
answer `503` so you can drive a retry test.

---

## `gossip/cases/` — the GossipConfig conformance cases

Each language writes **one generic runner** over these, not one test per case.

```jsonc
{
  "name": "merge_peers_exclude_beats_keep_existing",
  "description": "…",

  // exactly one of:
  "input_config":    { … } | null,   // a decoded config; null = start from nothing
  "input_json_text": "{…}",          // exact bytes to feed to parse() (malformed-input cases)

  "operations": [
    { "op": "merge_peers", "args": [ ["203.0.113.1"] ],
      "opts": { "mode": "replace", "keep_existing": ["192.0.2.10"] } }
  ],

  "expect": { … }                    // see below
}
```

### The runner

1. Build the subject. `input_json_text` present ⇒ `parse(text)`. `input_config`
   non-null ⇒ `parse(render(input_config))`, or construct directly from the decoded
   object — either is fine, they are equivalent. `input_config: null` ⇒ no subject
   yet; the first operation is then `new` or `from_peers`.
2. Apply each operation in order, in the language's idiomatic naming (`merge_peers`
   → `mergePeers` in TypeScript, and so on). `args` is a positional list; `opts` is
   the trailing options map.
3. Compare against `expect`.

`$TMPDIR` inside a path argument is substituted with a fresh empty directory,
created per case and removed afterwards.

### Operations

`new(chain, root_ips)` · `to_json()` · `validate()` · `save(path, opts?)` ·
`root_ips()` · `has_root_ip(ip)` · `add_root_ip(ip)` · `remove_root_ip(ip)` ·
`set_root_ips(ips)` · `reserved_peer_ips()` · `add_reserved_peer_ip(ip)` ·
`remove_reserved_peer_ip(ip)` · `set_reserved_peer_ips(ips)` ·
`set_chain(chain)` · `set_try_new_peers(bool)` · `set_n_gossip_peers(int)` ·
`set_split_client_blocks(bool)` · `unset_split_client_blocks()` ·
`merge_peers(peers, opts)` · `from_peers(peers, opts)`

`peers` accepts bare IPv4 strings or objects with an `id` — both appear in the
cases, because both are accepted on input.

### `expect`

| key | meaning |
|---|---|
| `json_text` | after the last operation, `to_json()` must equal this **byte for byte** |
| `value` | the **last** operation's return value equals this. For an object, **subset match**: only the keys present here are compared. `"ok"` means `validate()` succeeded |
| `error` | an error must be raised: `{kind, parameter?, at_op?, reason_contains?}` |
| `warnings` | **required on every non-error case.** See below |
| `diff_contains` | every string must appear in the returned diff |
| `file_after` | `{path, json_text}` — the file at `path` must hold exactly these bytes |
| `file_absent` | nothing may exist at this path |

`error.kind` is the taxonomy name from `CLIENT_SPEC.md` §5.1 (`Invalid`, `Decode`,
`NotFound`, …). `error.at_op` is the zero-based index of the operation that must
raise; when absent, any operation may. `error.reason_contains` is a substring of
the human reason and is matched case-insensitively — it pins the *fact* being
reported, not the wording; a client may phrase the rest however it likes.

### `expect.warnings` — exact, and mandatory

After the last operation the runner calls `validate()` and asserts that the
resulting warning **set** — §6.4's state-derived warnings unioned with any pending
merge warnings (`CLIENT_SPEC.md` §6.5), de-duplicated by code — equals
`expect.warnings`.

- It is **required on every non-error case**, and `[]` is the common value. An
  absent key is how an incidental warning hides from a set comparison, so
  `tools/check_fixtures.py` fails a non-error case that omits it.
- Comparison is a set comparison; the fixture lists codes sorted, and a runner may
  compare in any order.
- An **error** case must *not* assert warnings — nothing reached a valid state.
- Each code has exactly one case that isolates it. Cases testing something else
  use a `try_new_peers: true` base so that growing the root list past one entry
  does not drag in an incidental `try_new_peers_false_multi_root`.
- 🔴 **The codes are independent**, and two cases pin it: 25 roots with
  `try_new_peers: false` must report **both** `many_roots_gt_24` and
  `try_new_peers_false_multi_root`. A `many_roots_*` warning does not suppress the
  pinning warning. Only `many_roots_gt_24` / `many_roots_gt_64` are mutually
  exclusive.

**Coverage.** 76 cases: every method in §6.2; byte-exactness (key order, 2-space
indent, single trailing newline, empty-array inlining, literal UTF-8); unknown-key
preservation through parse, mutation and round-trip; the capital-`Ip` entry shape
and its rejections (bare string, lowercase `ip`, extra `port`); `reserved_peer_ips`
as bare strings and the rejection of a wrapped object there; dedupe; `max_roots`
truncation; `keep_existing`; `exclude` beating `keep_existing`; the three merge
modes; **the zero-roots refusal, in four different ways**; IPv6 rejection; malformed
IPv4; duplicates in either list; chain spelling; **all six warning codes, one
isolating case each, plus two proving they are independent**, plus a case proving
a merge warning survives to `save`;
malformed and non-object JSON input; `save` — idempotence, `dry_run`, refusal
to write an invalid config, and a byte-equivalent round trip of the live minimal
form; and **seven cross-client resolutions** — points where the four
implementations disagreed and `CLIENT_SPEC.md` was silent, found by
`tools/cross_check/` and now written into the spec: the `peers[<n>]`
parameter of an invalid merge candidate, mutators deferring IP and `chain`
validation to `validate()`, a no-op `remove_*` not creating the key, a merge
leaving `chain`/`try_new_peers` alone, the `dedupe: false` refusal landing after
truncation, and a merge validating its whole result.
