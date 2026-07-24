# Changelog

## Unreleased

- Areas now mount through a registry (`dataplat/core/registry.py`) — the
  single seam a future plugin mechanism will extend. Built-in areas declare
  the same `module:attr` target shape a package entry point would; no
  user-facing changes.

## 0.1.0

Initial public release.

- Optional per-area dependency extras (`dataplat[db,ingest,bi,cloud,all]`)
  with auto-detection: `dp config sync` installs what your enabled areas
  need, and hitting a stubbed area offers to install its extra and re-run
  your command.

- `dp` command with areas per component type: `db`, `ingest` (Airbyte),
  `bi` (Superset), `cloud` (AWS), `ci` (GitHub runners), plus `status`,
  `open`, and `config`.
- Fully config-driven database targets via `DP_TARGETS` and per-target
  `<NAME>_*` environment variables (Postgres and Redshift engines).
- `.envrc` loading with a global config link (`dp config init`).
