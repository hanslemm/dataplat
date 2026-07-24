# Changelog

## 0.1.0

Initial public release.

- `dp` command with areas per component type: `db`, `ingest` (Airbyte),
  `bi` (Superset), `cloud` (AWS), `ci` (GitHub runners), plus `status`,
  `open`, and `config`.
- Fully config-driven database targets via `DP_TARGETS` and per-target
  `<NAME>_*` environment variables (Postgres and Redshift engines).
- `.envrc` loading with a global config link (`dp config init`).
