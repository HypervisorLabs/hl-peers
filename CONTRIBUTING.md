# Contributing

## Where things are owned

| you are changing | do it in |
|---|---|
| behaviour every client shares | `CLIENT_SPEC.md`, **then** all four clients |
| the wire contract | upstream, then re-copy and re-redact (`spec/SOURCE.md`) |
| a fixture | `tools/gen_fixtures.py`, then regenerate — never by hand |
| one language | that language's directory, and nothing else |

🔴 **`spec/peer-api-v1.openapi.yaml` is a copy and is never edited here for
content** — only for the redactions `spec/SOURCE.md` lists, which strip producer
hostnames and internal paths. If the spec is wrong, fix it upstream. If the spec is *ambiguous for a client
author*, that is not a spec bug — resolve it in `CLIENT_SPEC.md` §10 and list the
decision there, so all four clients resolve it the same way.

## The loop

```bash
python3 tools/gen_fixtures.py      # only if you changed the generator
python3 tools/check_fixtures.py    # schema + byte rules + documentation-IP check
npx @redocly/cli lint spec/peer-api-v1.openapi.yaml
python3 tools/mock_server.py --port 8931 &
# then the per-language test command from that directory's README
```

## Rules that are not negotiable

1. **Only RFC 5737 documentation addresses** — `192.0.2.0/24`, `198.51.100.0/24`,
   `203.0.113.0/24`. No real peer IP, no fleet IP, no hostname of a real box, ever.
   `tools/check_fixtures.py` fails the build on a violation.
2. **The default base URL is the public API host** (`https://api.hypervisor.gg`,
   one constant per client), and **no test may reach it** (rule 3).
3. **No network in any default test run.** Fixtures and the mock server, always.
4. **Never weaken the zero-roots refusal.** A change that lets a client emit an
   empty `root_node_ips` takes an operator's node down. The four conformance cases
   pinning it are not to be skipped, xfailed, or "temporarily" relaxed.
5. **No client ever restarts, signals or reloads a node.** No `--restart` flag, no
   `apply()` that does one, no "convenience" `systemctl`.
6. **Three-state enums are never folded.** No accessor may collapse `unknown` into
   a boolean false.
7. **Carry "unverified" through.** Where the node's behaviour is unverified, the
   client refuses or warns; it does not quietly pick an answer and it does not
   present a guess as a fact in a docstring.

## Adding a conformance case

Add it to `gossip_cases()` (or `http_fixtures()`) in `tools/gen_fixtures.py`,
regenerate, and run `tools/check_fixtures.py`. A new case is a change to all four
clients' test suites at once — which is the point. Write the `description` for
someone reading the failure six months from now: say *why the case exists*, not
what the assertion is.

## Style

`.editorconfig` governs whitespace. Each language uses its own standard formatter
and linter, listed in its directory README, and both must pass in CI. Markdown
wraps at 80 columns.

## Commits

Conventional-ish prefixes: `spec:`, `fixtures:`, `docs:`, `elixir:`, `ts:`,
`rust:`, `python:`, `ci:`. Say what changed and why; the diff already says how.
