# Releasing

All four packages share one version and are released together by
`.github/workflows/release.yml`. Nobody publishes from a laptop.

| package | registry | credential the workflow needs |
|---|---|---|
| `typescript/` `hl-peers` | npm | secret `NPM_TOKEN` (automation token; published with provenance) |
| `rust/` `hl-peers` | crates.io | secret `CARGO_REGISTRY_TOKEN` |
| `python/` `hl-peers` | PyPI | variable `HL_PEERS_PYPI_TRUSTED=true`, and the PyPI project trusts `HypervisorLabs/hl-peers`, workflow `release.yml` (trusted publishing, no token) |
| `elixir/` `hl_peers` | Hex | secret `HEX_API_KEY` |

## The gates

A registry is published to only when **all** of these hold:

1. the run is for a `v*.*.*` tag. A manual run (`workflow_dispatch`) is always a dry run;
2. the repository variable `HL_PEERS_PUBLISH` is exactly `true`;
3. that registry's credential above is present.

Otherwise its publish job is skipped and the run summary says so. The tag must
equal every package's version, or the run fails before anything else. The full
CI gate set (`ci.yml`) runs first, then each package is built once; the publish
jobs take those built artifacts.

## Cutting a release

1. Bump the version in `typescript/package.json` (and `package-lock.json`),
   `typescript/src/client.ts`, `rust/Cargo.toml` (and `Cargo.lock`),
   `python/pyproject.toml`, `python/src/hl_peers/{__init__,_http}.py`,
   `elixir/mix.exs`, the user-agent tests and the version lines in the READMEs
   and `docs/`. `grep -rn '<old version>'` finds them.
2. Move each changelog's Unreleased entries under the new version.
3. Run `python3 tools/gen_fixtures.py` (fixtures must not change) and every
   language's gates (each README lists them). Merge to `main` and wait for CI.
4. Only when publishing is wanted: set the credentials and `HL_PEERS_PUBLISH`,
   then push the tag: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`.
5. Afterwards, set `HL_PEERS_PUBLISH` back to anything but `true` if the next
   tag should not publish.

A registry that failed can be retried by re-running its job from the run page.
Published versions cannot be replaced: fix forward with a new patch version.
