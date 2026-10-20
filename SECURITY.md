# Security policy

## Status

The latest 1.x release is the supported version.

## Reporting

Report suspected vulnerabilities privately to **hypervisorlabs@gmail.com**. Do not
open a public issue for a vulnerability.

Please include what you did, what happened, and what you expected. We will
acknowledge and give you a timeline; please give us a reasonable window before
disclosing.

## What counts as a vulnerability here

These are client libraries and a config generator, so the interesting surface is
narrow but sharp:

- **Anything that can make a client emit or save an empty `root_node_ips`.** That
  crash-loops an operator's node. It is a denial-of-service against a user's
  infrastructure and it is treated as the most serious class of bug in this
  repository, ahead of anything in the HTTP layer.
- **Anything that writes outside the path the caller asked for**, including path
  traversal through a config path, a symlink followed on write, or a temp file
  left readable.
- **A widened file mode.** The config names infrastructure an operator peers with;
  `save` must never widen an existing mode, and creates new files `0600`.
- **API-key leakage** — a key in a log line, an exception message, a query string,
  a `User-Agent`, a cached filename, or a crash dump.
- **Deserialization or resource bugs** reachable from an API response: unbounded
  memory on a large or malicious body, a decompression bomb, a cursor loop that
  never terminates.
- **A dependency with a known advisory** reachable from a default build.

## What does not

- The `*.example.invalid` hostnames in tests and examples. They are deliberate.
- The absence of TLS pinning. Clients use the platform trust store.
- The upstream API's own rate limits, privacy posture, or the operator opt-out
  question. Those belong to the API, not to these wrappers.
- The documentation IP addresses in `fixtures/`. They are RFC 5737 by policy, and
  a real address appearing there is a **policy violation to report**, not a
  vulnerability in the usual sense — though we do want to hear about it
  immediately.

## Handling keys

`HL_PEER_API_KEY` is read from the environment. Clients never log it, never place
it in a URL, and redact it in diagnostics. Anonymous access is a first-class tier:
if you do not need a key, do not hold one.
