# Changelog

## 0.2.3

Redshift-only fixes. Nothing changes for PostgreSQL targets.

### Fixed

- `dp db role show` no longer claims every Redshift user has no password. The
  attribute query reported `password_set=False` unconditionally, but
  `pg_user.passwd` is masked to `'********'` there just as `pg_roles.rolpassword`
  is on PostgreSQL — so it asserted "this login has no password" for every user,
  the same falsehood 0.2.2 fixed on the PostgreSQL side. It now reports
  `unknown`, with the reason. A Redshift *group* still reports `no`, because a
  group has no password to hold.

- `dp db describe <schema>` now reports `USAGE` grants on Redshift. The query
  read `information_schema.usage_privileges` filtered to `object_type = 'SCHEMA'`,
  which the SQL standard defines over domains, collations and sequences — never
  schemas — so it returned nothing on every server. It now scans
  `has_schema_privilege`, mirroring how the same query has always reported
  `CREATE` on that path. As with `CREATE`, a privilege scan cannot report a
  grantor or a grant option, so both stay empty; the PostgreSQL path reads the
  ACL and does better on both counts.

  Both fixes rest on documented behaviour and internal precedent rather than a
  live cluster — Redshift cannot be containerized, so CI cannot cover it. See
  below.

### Added

- A Redshift conformance harness (`tests/integration/redshift/`) for anyone who
  runs dataplat against a real cluster. The read-only tier is safe to point at a
  warehouse in use — a guard refuses anything that is not plainly a read before
  it reaches the server — and it interrogates the assumptions the two fixes above
  depend on, printing what your cluster answered. `CONTRIBUTING.md` documents
  both tiers and the evidence rules for changing SQL that runs on a dialect CI
  cannot reach.

## 0.2.2

Closes the six defects 0.2.1's integration suite found and pinned as expected
failures. No expected failures remain.

### Fixed

- `dp db role show` claimed `Password set: yes` for every role, including a
  passwordless `NOLOGIN` group. The attribute query read `rolpassword` from
  `pg_roles`, whose view definition returns the literal `'********'` and can
  never be NULL. Only `pg_authid` holds the real verifier and it is
  superuser-only, so the field is now tri-state and reports `unknown` — with a
  hint saying why — when the connecting role cannot read it. The privilege is
  probed before the query rather than discovered by catching an error, since a
  permission failure would abort the surrounding transaction.

- `dp db describe <schema>` never reported `USAGE` grants. The query read
  `information_schema.usage_privileges`, which on PostgreSQL does not cover
  schemas at all, so the USAGE half of the union always returned nothing. The
  PostgreSQL path now reads the schema ACL directly.

- `dp db role show` under-counted a role's tables when it owned a partitioned
  table. Both `relkind` `'r'` and `'p'` map to the label "table" and the
  aggregation assigned rather than accumulated, so one group overwrote the
  other while the total was summed separately and stayed right — the per-schema
  breakdown contradicted its own total.

- `dp db dbt-orphans purge` aborted the whole batch when a single relation had
  vanished between scan and purge. The generated statement now uses
  `IF EXISTS`, matching `top-tables`, so a missing relation is a no-op. When a
  dependent object genuinely blocks a drop the purge still stops — that
  all-or-nothing property is deliberate for a destructive batch — but it now
  names the relation and everything depending on it instead of surfacing a raw
  driver error, and records the blockage in the audit log.

- `dp db describe <relation>` raised nothing but returned an invalid result for
  a non-view relation: `pg_get_viewdef()` yields NULL rather than erroring, and
  the PostgreSQL branch returned a view definition whose `sql` was `None`,
  violating its own annotation. It now raises, as the Redshift branch already
  did.

- `dp db long-queries --history` died with a driver traceback on any server
  where `pg_stat_statements` is installed but not preloaded — the most common
  misconfiguration. The guard that was meant to catch this probed the view's
  columns, which PostgreSQL answers from the view definition without invoking
  the extension, so the probe always succeeded. The failure is now reported as
  an actionable error naming `shared_preload_libraries` and the required
  restart.

### Changed

- `RoleAttributes.password_set` widened from `bool` to `bool | None`, where
  `None` means "not determinable by this connection". Relevant only if you
  import the dataclass; the CLI renders the third state as `unknown`.

- On PostgreSQL, `dp db describe <schema>` privileges now come from the schema
  ACL rather than a `has_schema_privilege` scan. A role holding `CREATE` only
  through membership in a granted role is no longer listed as its own row, and
  `grantor` and `WITH GRANT OPTION` are now real values where the previous
  CREATE half hardcoded them.

## 0.2.1

### Fixed

- `dp db dbt-orphans` could report — and with `purge --include-unknown`, drop —
  a relation it had never renamed. The scan matched `LIKE '%_deprecated'` with
  no `ESCAPE`, and an unescaped `_` matches any single character, so a table
  named `legacydeprecated` came back as a purge candidate. Found by running the
  scan against a real PostgreSQL; `top_tables.py` had carried the escaping fix
  for this since the beginning, one module over, which is exactly why the
  helper now lives in one place both modules share.

- `dp db describe` reported `Size 0 B` and no row estimate for every ordinary
  table, matview and view. All five header size aggregates read from
  `pg_partition_tree()`, which returns no rows for a relation that is neither
  partitioned nor a partition; `SUM` over no rows is `NULL`, and a
  `COALESCE(..., 0)` inside each subquery presented that as a confident zero.
  Partitioned tables had the mirror-image bug and double-counted rows, because
  `reltuples` on an analyzed parent already aggregates its partitions.

- `dp db role show` under-reported a role's privileges. The recursive
  membership walk collapsed duplicate paths with `DISTINCT ON`, and when an
  ancestor was reachable by several equal-depth paths the surviving row — and
  therefore whether `INHERIT` was honoured — was decided by scan order.
  PostgreSQL reported the privilege as held while dataplat showed it missing.

- `dp db long-queries` could return an empty snapshot. The scan measured age
  with `now()`, which is `transaction_timestamp()`, so any query that started
  after the scanning transaction began had a negative age and failed the
  threshold filter silently.

### Added

- An integration suite that executes the database service layer against a real
  PostgreSQL, in CI and locally (`tests/integration/`, see the README). It
  found all four fixes above, plus seven further defects now pinned as strict
  expected failures. Releases are gated on it: `build` will not run until the
  suite passes.

## 0.2.0

### Fixed

- Data containing square brackets no longer breaks output. Rich reads `[...]`
  as markup in every string it renders, so a warehouse value, identifier,
  comment, secret, API payload, or driver message containing `[/x]` aborted the
  command with a `MarkupError` traceback, and one containing a style name like
  `[bold]` was silently swallowed — the output quietly misreported the data.
  Every renderer now emits external values as literal text, table *headers*
  included: `dp ingest airbyte connections list --all-columns` takes its column
  labels straight from Airbyte's JSON keys, and the Textual TUI passes the same
  labels to a `DataTable`, so both had the identical failure.

- `dp db dbt-orphans revert` and `purge --older-than` could not find their own
  audit logs if the log directory's path contained a bracket — a home directory
  named `[work]` was enough. The discovery glob interpolated the directory
  without escaping it, so the brackets read as a character class and matched
  nothing: `revert` reported no history, and `purge --older-than` treated every
  object as having no recorded rename.

- Installing a missing extra no longer changes anything else about your
  install. It was resolved unpinned, so hitting a stubbed area could upgrade
  `dataplat` itself and then re-exec your command against the new code; and
  because `--force` rebuilds the environment from the spec it is given,
  installing `db` silently removed an `ingest` you already had. The command is
  now pinned to the running version and unions the extras already present.

- `dp ci github runner start|stop|status` identified containers with an
  unanchored `docker --filter name=`, which is a substring match: a runner
  named `foo` matched `gha-runner-foobar`, so `status` could report a
  different runner's state as yours. All filters are anchored.

- `dp status --json` emitted valid JSON only while nothing needed saying. An
  expired SSO token wrote its notice to stdout, mixed into the document.
  Spinners and notices now go to stderr; stdout carries only the payload.

- Pressing TAB inside an area whose extra is missing misbehaved badly. Shell
  completion walks the command tree with click's resilient parsing, which calls
  the same resolution hook the missing-extra stub overrode, so completion ran
  the interactive installer: the offer text landed on the stream the shell
  evaluates (zsh even re-invoked `dp` from the backticks in the message), a
  full traceback went to the terminal, and under bash the install itself
  executed from a keypress. Completion now falls through silently.

- Installing an extra inside a virtualenv that has no `pip` — the default for
  `uv venv` — prescribed `python -m pip install` and could only fail, reporting
  just a bare exit code. It now uses `uv pip install --python <env>` when pip is
  absent, and says so plainly when neither installer is reachable.

- The `Will run:` line is shell-quoted. It prints a command the user may want
  to run themselves, but the requirement spec's brackets (and now its `==`
  pin) meant pasting it into zsh failed on globbing.

### Added

- `DP_ENVRC_ALLOW_CWD=0` stops `.envrc` being picked up from the current
  directory. Loading it made every command sensitive to where you stood, with
  no way to see that had happened: `dp config show` and `dp config doctor` now
  name the active file and which candidate produced it, and warn when it came
  from the working directory.

- `DP_AWS_PROFILE_ALIASES` now works across the whole `dp cloud aws` group.
  Only `secrets` resolved aliases, so `dp cloud aws rds metrics -p prod` passed
  the literal `prod` to boto3 and failed with a profile-not-found error while
  the same flag worked one command over. `rds list` and `redshift metrics` also
  print the profile they actually used rather than the shorthand you typed.

- Python 3.12 support. `requires-python` was 3.13 while the only construct
  above 3.11 in the package is one PEP 695 generic, so 3.12 users hit an
  install failure for nothing. CI now tests the advertised floor as well as
  the dev pin.

- The release workflow verifies that the pushed tag matches
  `[project].version` and runs the full check matrix before building. CI also
  runs on tag pushes — a tag matched no branch filter, so releases were
  published without any checks having run.

### Changed

- Areas mount lazily, cutting `dp --version` from 360 ms to 100 ms. Every
  area's dependencies — psycopg, textual, httpx, boto3 — were imported at
  startup regardless of the command; now nothing heavy loads until the area
  that needs it is actually used. Areas resolve through the registry
  (`dataplat/core/registry.py`) and carry their own dependency contract, so a
  future third-party area is no longer tied to a lookup in a built-in dict.

- One confirmation gate covers every destructive command. Three idioms had
  drifted apart and only one told you what to do when stdin was not a TTY;
  non-interactive runs now consistently name the flag that would have worked
  and exit 1 rather than aborting with no explanation.

  **Behaviour change worth knowing before you upgrade:** piping a confirmation
  into a destructive command no longer authorizes it. `echo y | dp db role drop
  old_user` used to be accepted by the eight gates that had no TTY check, and
  now exits 1 naming `--yes`. Data-destroying work should not proceed on a `y`
  that arrived from a pipe rather than a person, and `--yes` has always been
  the documented scriptable path — but any script relying on the old behaviour
  fails loudly and needs `--yes` instead. Sites that already refused a piped
  answer (`dp db query`'s write guard, the AWS secrets writes) are unchanged.

- `line-length = 88` is enforced. It was configured but unchecked, because
  pycodestyle was not among the selected rule sets, and the tree had drifted
  to 164 violations across 71 unformatted files. The repo is now
  `ruff format`-clean and CI keeps it that way.

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
