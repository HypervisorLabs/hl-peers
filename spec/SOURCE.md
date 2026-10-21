# Where `peer-api-v1.openapi.yaml` comes from

`spec/peer-api-v1.openapi.yaml` is a copy of the producer's public Peer API
OpenAPI document, the one served at `https://api.hypervisor.gg/openapi.yaml`. It
is **not** edited here for API content (paths, parameters, schemas) — only
redacted, as described below.

| fact | value |
|---|---|
| source | the public Peer API OpenAPI document, as served at `/openapi.yaml` |
| copied on | 2026-10-19 |
| `info.version` | `1.0.0` (the API document's own version, not the clients') |
| `info.license` | MIT, like the rest of this repository; the one field that differs from the source |

One served shape is not in the source schema: `/network-map/summary` also returns
the graph's `cities` / `attribution` (`CLIENT_SPEC.md` §10 D-S).
`tools/gen_spec_fields.py` adds them to the generated field list with a note.

## 🔴 Redactions

The copy is redacted before it is committed, and **a refresh must redo every
redaction** — `tools/check_fixtures.py` will not catch a missed one for you.

- **Operational detail:** producer hostnames, internal table names, process
  inspection paths, and cross-references to documents that do not exist in this
  repository are rewritten generically or repointed at this repo's own `docs/`.
- **Measurement detail:** every *description* (`info.description`, parameter and
  schema descriptions, example summaries) is written at the user level. Removed:
  network ports and wire-protocol detail; how the scan discovers and checks
  peers (methods, intervals, timeouts, and how each state, `connectable`, `tier`,
  the mempool verdict and the geo confidence are determined); node and relay
  internals and tuning numbers.

Every field keeps its meaning for a caller ("`listening`: accepts inbound
connections, other details unknown"). Paths, parameter and property names,
types, enums, `required` lists and patterns are unchanged, so
`tools/gen_spec_fields.py --check` still passes. `spec/gossip-config.schema.json`
had the same pass.

The `override_gossip_config.json` contract and the API's rate limits and tiers are
restated, in public-safe form, in `CLIENT_SPEC.md` and `docs/gossip-config.md`.

## Refreshing the copy

```bash
curl -s https://api.hypervisor.gg/openapi.yaml > spec/peer-api-v1.openapi.yaml
# 🔴 redo the redactions above, and set info.license back to MIT
npx --yes @redocly/cli@latest lint spec/peer-api-v1.openapi.yaml
grep -nEi 'hlnode|/proc/|docs/plans/|docs/api/|nw\.|:400[12]|\b400[12]\b|dial|greeting|variant|prober|BFS|round-trip (physics|measure)|stock inbound|subscri|crawl' spec/peer-api-v1.openapi.yaml spec/gossip-config.schema.json   # must be empty
python3 tools/gen_spec_fields.py && python3 tools/gen_fixtures.py && python3 tools/check_fixtures.py
```

Never hand-edit the copy for *content*. If the spec is wrong, it is fixed at the
source and re-copied. If the spec is *ambiguous* for a client author, the
resolution belongs in `CLIENT_SPEC.md` §10 "Resolved ambiguities", not in this
file.

## Status

The API is live at `https://api.hypervisor.gg` (the clients' default base URL).
The document lists that host in `servers`, its problem `type` URIs and
`signup_url`; clients match problem types by their last path segment, so the host
does not matter to them. Every client is tested against `fixtures/` and
`tools/mock_server.py` only — the default test run never touches the network.
