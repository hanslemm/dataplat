# Changelog

## Unreleased

### Added

- **Tests for the Airbyte service layer**, which was the largest untested area
  left after coverage measurement arrived: 64% → 89% across those modules, with
  `tags.py` going 36% → 97% and `connections.py` 53% → 80%.

  Nothing here executes against a real Airbyte, and the tests say so at the top of
  each module. What a fake can prove is what *this* code does with a given
  response — which is the whole subject, because these branches exist to absorb
  the shape variance the API is known to produce (`tagId` or `id`, `workspaceId`
  or `workspace_id`, a bare list or a dict wrapping one) and none of them had ever
  run.

  Two latent risks are now pinned by tests that name them and deliberately do
  **not** change behaviour, because choosing between the alternatives needs an
  Airbyte to ask and there is none:

  - `merge_tags` silently drops a tag carrying no id. The merged list is written
    back as a connection's *complete* tag set, so an entry it cannot identify is
    not merely unmerged — it is removed, and adding one tag would delete another.
  - `TagResolver` caches by the workspace found in the payload but looks up by the
    workspace it was asked about, so a listing that omits `workspaceId` recreates
    a tag that already exists.

  No defects were found in `client.py`'s helpers — `build_auth_headers`,
  `parse_jwt_exp` and the cron pair were correct in every branch, including the
  subtle ones (an empty `AIRBYTE_AUTH_VALUE` is honoured rather than falling back
  to a credential the operator cleared; a trailing field that does not resolve as
  a timezone is left attached rather than silently dropped). They were simply
  untested, and four environment variables that decide what credential goes on
  every request now have tests.

### Fixed

- **`dp db role list` failed outright on a PostgreSQL server with
  `standard_conforming_strings` off.** `_LIST_ROLES_SQL` hid system roles with
  `NOT LIKE 'pg\_%' ESCAPE '\'`, and with that setting off the backslash escapes
  its own closing quote, so the statement raised `unterminated quoted string`
  before returning a row. Correct on a default server, which is why it survived —
  PostgreSQL has defaulted the setting to `on` since 9.1. Now `#`, matching the
  rest of the tree.

  Only PostgreSQL reached that query, so this was never a Redshift bug. It is the
  third instance of the same class, after the `top-tables`/`dbt-orphans` escape and
  the `schema drop --like` over-match.

### Changed

- **CI measures coverage**, combined across jobs, with a floor that fails the
  build on a regression. There was no measurement at all before.

  It has to be combined because no single job runs the whole suite: `checks`
  deselects the tier that needs a server and `integration` deselects everything
  else. Either figure alone is wrong in a way that looks plausible — the unit leg
  reads 87%, the integration leg 30%, and the union is what the suite exercises.
  The number is rendered into the run summary so a reviewer sees it without
  opening a log.

  `fail_under` is a floor for catching a test file that stopped running or a
  sizeable module that arrived without tests, not a target. `pyproject.toml` and
  `CONTRIBUTING.md` both say so at the point of use, with the reason: the
  `ESCAPE '\'` that broke `dp db top-tables` on every Redshift target for five
  releases was covered the entire time it was broken.

  Measuring it immediately found two gaps in code shipped days earlier, both now
  closed:

  - **`dp db schema alter`'s entire happy path was untested** — every existing
    test for it asserted a refusal, so all of them returned before the plan was
    built. Build, print, confirm and execute had never run outside a manual
    check. 66% → 100%.
  - **`_held_identity_pin` had no tests at all.** It is the predicate that keeps a
    Redshift group's privileges from being merged with a same-named role's, which
    is a security-relevant rule and pure logic needing no server. Now covered
    arm by arm, including that `PUBLIC` is never bound as a name and that an
    unresolved kind matches nothing rather than falling back to a name-only match.

- **One savepoint guard instead of four.** Probing for a catalog that may not
  exist — Redshift's `svv_*` views are version-dependent and can be
  permission-denied — needs a savepoint, because a failed probe inside a
  transaction aborts the whole transaction and would discard work the caller had
  already done. Three call sites hand-rolled that pattern next to the shared
  `guarded_fetch` that now does it, and four copies of a subtle transaction-safety
  routine is how one of them eventually loses its rollback.

  The `None` vs `[]` distinction in `guarded_fetch`'s return is what made the
  consolidation possible rather than a lossy merge: `_redshift_rbac_available`
  must tell "view absent" from "view present and empty", because its probe is
  `LIMIT 0` and succeeds with no rows, while the other two callers legitimately
  collapse both into "nothing".

  Also renames two savepoints that carried the upstream tool's `dna_` prefix —
  `dp db` users were finding `dna_rbac_probe` in their PostgreSQL logs — and the
  tests that asserted the old name now reference the constant instead.

  The property all of this exists for is now covered against a real server for the
  first time. The unit tests use fakes that swallow SAVEPOINT and ROLLBACK, so they
  can show the statements were issued and never that the transaction survived;
  PostgreSQL has no `svv_*` views at all, which makes it a free stand-in for the
  pre-RBAC cluster these probes are for. Verified by mutation: removing the
  savepoint fails all four new tests.

- **CI runs the whole PostgreSQL integration tier twice**, once with
  `standard_conforming_strings` on and once off — the setting every Redshift
  cluster runs with, and the one documented Redshift behaviour reproducible on a
  server CI can actually have.

  It is not a Redshift emulation and is documented not to be read as one: Redshift
  also has an 8.0.2 leader node, no `aclexplode()` and different catalogs, none of
  which a setting reproduces. What the leg proves is narrower — that no SQL a
  PostgreSQL target runs depends on that setting — and it is a standing guard on
  the one bug class that has escaped this project three times now. It found the
  `role list` failure above on its first run.

  Two Postgres-only regexes moved from `'\s+'` to `'[[:space:]]+'` in the process:
  no backslash at all, so they behave identically whatever the setting is. The
  Redshift-only regex in `build_long_queries_query` keeps its doubled backslash,
  which is correct for that server, and now carries a comment saying why it differs
  and what the wrong backslash count would cost — measured on PostgreSQL 16,
  `SELECT * FROM users` renders as `SELECT * FROM u er`, silently corrupting the
  SQL an operator reads to decide what to kill.

## 0.5.0

### Added

- **`dp db schema list` — schemas with owner and object counts**, on all three
  engines, with `--like` (glob `*` or SQL `%`; `_` is literal),
  `--include-system` and `--json`.
  No refusal for DuckDB: it has schemas, so it gets an answer.

  The three engines need three statements, not one with a flag. PostgreSQL
  resolves the owner through `pg_roles`, Redshift through `pg_user`, and DuckDB
  has no `pg_roles` at all — measured, not assumed, and pinned by a test that
  fails if a future DuckDB grows one. Redshift adds quota columns from
  `svv_schema_quota_state`; that view is version-dependent, so an unavailable one
  degrades every quota to `?` rather than failing the listing, and never to `0`,
  which would read as "no limit".

  Object counts bucket every relkind a drop would destroy — tables, partitioned
  and foreign tables, views and materialized views, and sequences and composite
  types in `other` — so nothing a future `schema drop` pre-flight cares about can
  go uncounted.

- **The rest of `dp db schema`: `create`, `drop`, `grant`, `revoke`, `alter`.**

  `list`, `create` and `drop` work on all three engines. `grant`, `revoke` and
  `alter` need a server, and DuckDB refuses them with the reason measured from the
  engine: it has no `GRANT` statement at all (the keyword does not parse) and does
  not implement `ALTER SCHEMA` ("Altering schemas is not yet supported").
  `create --owner` is refused there too — `AUTHORIZATION` does not parse.

  **Privileges** take `usage`, `create`, `all`, `select`, `insert`, `update`,
  `delete`, `table-all`, `sequence-usage`, `function-execute`, `default-select`,
  `default-all`, or the presets `read` / `readwrite`. Use
  `--grant grantee:privileges` when two grantees need different things in one
  invocation. `PUBLIC` is valid here, unlike in `role grant` — an object privilege
  to PUBLIC is ordinary SQL, while role membership to it is not.

  Three behaviours that exist to stop a grant that silently does nothing:

  - Any table-level privilege implies `usage` on the containing schema.
  - `default-*` privileges require a grantor. `ALTER DEFAULT PRIVILEGES` without
    `FOR ROLE`/`FOR USER` binds to whoever is connected, so tables later created
    by dbt or the schema owner inherit nothing. Each schema's own owner is the
    default; `--default-for` overrides. An integration test creates a table *as*
    the grantor and asserts the grantee can actually select from it.
  - Grants already in effect are reported, not re-issued, so re-running converges.

  **Destructive paths.** `drop` prints owner and object counts before the
  confirmation, so `--cascade`'s blast radius is visible rather than implied, and
  warns up front when `RESTRICT` will refuse. `RESTRICT` is emitted explicitly
  rather than left to the server default. `drop` and `alter` refuse `public`,
  `main`, `information_schema`, `catalog_history` and anything `pg_*` — matched
  with `casefold()`, and re-checked after `--like` expansion, because
  `list_schemas` hides `pg_*` but not `public`.

  A quota is neither an identifier nor a bindable parameter, so it is interpolated
  into DDL text; `parse_quota` rebuilds it from parsed regex groups, and
  `CreateSchemaSpec` re-normalizes so that holds for library callers too.

  Every one of the twelve privilege statements, plus create/drop/alter, is
  executed against a live PostgreSQL 16 rather than asserted against a fake.

- **`dp db role grant` — grant existing roles to users/roles in one pass.**
  `role create` can wire up membership for a role it is creating; this is the
  command for every day after that. It takes the cross product of `--roles` and
  `--to`, so onboarding three people to two roles is one invocation.

  It validates the whole plan before executing any of it, so a typo in the last
  `--to` fails before the first user is created. Grants already in effect are
  reported and skipped rather than re-issued, which makes the command safe to
  re-run after a partial failure. `--create-missing-users` creates any `--to`
  name that does not exist yet as a login user, writing generated passwords to
  the same `0600` CSV `role create` uses; creates and grants share one
  transaction, so a failed grant leaves no half-onboarded user behind.

  Three combinations are refused by name instead of surfacing as a raw SQL error
  partway through a batch: adding a role to a Redshift group, granting a role
  *to* a Redshift group (there is no `GRANT ROLE ... TO GROUP` form), and
  granting a Redshift login user to anything (only roles and groups hold
  members). On Redshift one name can be a user *and* a group *and* a role at
  once — `--kind` / `--to-kind` disambiguate, and an ambiguous name is refused
  rather than guessed. `PUBLIC` is refused too: verified on PostgreSQL 16,
  `GRANT <role> TO PUBLIC` fails with `role "public" does not exist`, and
  catching it early matters because `--create-missing-users` would otherwise try
  to create a user called `PUBLIC`.

  Ported from the upstream `dna-hq-cli`, with the Redshift-user refusal and the
  `PUBLIC` refusal added here. The two catalog reads it needs — "what kind of
  object is this name" and "which grants are already held" — are covered against
  a live PostgreSQL server, not just fakes.

### Fixed

- **`dp db schema drop --like` could destroy a schema nobody named.** `--like`
  translated glob `*` to SQL `%` and left `_` alone — but `_` is a
  single-character wildcard in `LIKE`, so `dev_*` also matched `devops_prod`. With
  `--cascade --yes` that dropped it and everything in it.

  Underscores in a `--like` pattern are now escaped and the statement declares
  `ESCAPE '#'`; a typed `%` is still a wildcard, so `dev_*` and `dev_%` remain
  equivalent. Applies to `list`, `drop`, `grant` and `revoke`. Pinned against
  PostgreSQL 16 and DuckDB from both sides: the escaped pattern selects only what
  was asked for, and the unescaped one demonstrably over-matches.

  Same lesson as the `top-tables`/`dbt-orphans` escaping fix below, reintroduced
  one command further along — which is why the shared helper now lives next to
  `like_escape` rather than in the schema module, so the next caller has somewhere
  obvious to reach for.

- **`dp db dbt-orphans` silently skipped schemas whose names start with `pg` plus
  one character.** The system-schema exclusion was `table_schema NOT LIKE 'pg_%'`
  with no `ESCAPE`, so it swallowed `pgx_staging` and `pgbouncer_meta` along with
  `pg_catalog` — and an orphan in one of those was invisible to the scan. A false
  negative, which is why it went unnoticed: the command reported fewer candidates
  and nothing said any were missing. Confirmed on PostgreSQL 16, where a
  `pgx_staging.orders_deprecated` returned no rows before this.

- **`dp db role create --no-login` created `~/.config/dataplat/credentials/`
  even though it generates no passwords.** The default credentials path is now
  resolved inside the branch that needs it, so a command with no secret to write
  no longer creates a directory to write it to. Same for `role grant --dry-run`.

- **`dp db top-tables` and `dp db dbt-orphans` were broken on every Redshift
  target.** Both build a `LIKE` pattern and declare `ESCAPE '\'`, but Redshift
  runs with `standard_conforming_strings` off, so that backslash escapes its own
  closing quote and the statement never parses. The escape character is now `#`,
  which has no special meaning to either engine's string-literal parser.

  This affected 0.2.1 through 0.4.0 and no test caught it, because PostgreSQL has
  the setting on and parses the identical text happily. It is now pinned by a
  test that opens a PostgreSQL session with the Redshift setting and asserts both
  that the new clause parses and that the old one raises
  `unterminated quoted string` — the closest thing to Redshift coverage available
  without a cluster.

  Credit where due: found by reading the upstream `dna-hq-cli`, which hit it for
  real and fixed it first.

## 0.4.0

### Added

- **DuckDB is a supported engine.** Set `<NAME>_ENGINE=duckdb` and
  `<NAME>_PATH=/path/to.duckdb` (or `:memory:`), and install
  `dataplat[duckdb]`. `<NAME>_DATABASE` works as a fallback for the path, and
  `<NAME>_READ_ONLY=1` opens the file read-only.

  DuckDB is not a smaller PostgreSQL — it runs inside the `dp` process, backed by
  a file, with a single implicit user — so only the commands that mean something
  there are available:

  | command | DuckDB | why |
  | --- | --- | --- |
  | `db query` | works | |
  | `db describe` | works | sizes and view definitions come from DuckDB's own catalog |
  | `db top-tables` | works | sizes are DuckDB estimates, not comparable to PostgreSQL's |
  | `db role *` | refused | there are no users or roles to describe |
  | `db long-queries`, `db kill` | refused | in-process: there are no other sessions |
  | `db dbt-orphans` | refused | it works by renaming, and DuckDB refuses to rename a relation a view depends on |

  A refused command exits 2 and says which engine and why. These are properties
  of the database, not gaps — the messages say so, because a user deserves to
  know a thing can never work rather than assume it is coming.

  `db describe` reports what DuckDB has no concept of — materialized views,
  partitions, triggers, row-level security, privileges — as not applicable, with
  the reason, rather than as empty sections that would read as "you have none".

  Unlike PostgreSQL (needs a container) and Redshift (needs a cluster), the
  DuckDB test tier needs nothing at all, so it never skips: this is the first
  dialect with unconditional executing coverage in CI, and the second real-SQL
  target the shared queries have ever been checked against.

### Fixed

- `dp config doctor` reported failures for a valid DuckDB target: it demanded
  `<NAME>_HOST`, `<NAME>_USER` and `<NAME>_PASSWORD`, then failed the connection
  check by dialing a server that does not exist. It now asks for the variables
  the engine actually uses, and probes a DuckDB target by opening its file —
  which is the health check.

## 0.3.0

### Changed

- **Exit codes now say what went wrong.** Every failure used to be `1`, so a
  wrapper script could not tell "your config is wrong" from "the warehouse is
  down" — and only one of those is worth retrying. Typed failures now carry
  their own code: `2` invalid input, `3` configuration, `4` authentication,
  `5` external service. `0`, `1` and `2` keep their conventional meanings, and
  `2` is deliberately shared with Click's own usage error, because
  `--format nope` and `-t nosuchtarget` are one condition to the caller.

  An unreachable warehouse exits `5`, since that is the retryable case. A bad
  statement against a reachable server stays `1`: retrying a syntax error would
  fail identically forever. Untyped failures and a declined confirmation also
  stay `1`. The full table is in the README.

  **Scripts that branch on a non-zero exit will see new numbers.** Anything
  testing `== 1` for a config or auth problem needs updating.

- **`dp db dbt-orphans` is more aggressive.** Its "which models are live" query
  interpolated the dbt project name into a `LIKE` pattern without escaping it,
  and dbt project names are snake_case — so the `_` in `my_project` matched any
  character and a *sibling* project's models (`my2project`) sharing the
  `dbt_artifacts` schema were counted as live. The same applied to
  `DP_DBT_INVOCATION_COMMAND`, where `_` is ordinary in a dbt selector.

  The direction is the point: an over-large live set makes **fewer** objects look
  orphaned. Escaping it shrinks the live set, so dbt-orphans will now rename —
  and after the grace period drop — objects it previously left alone. Run
  `dp db dbt-orphans` (dry-run is the default) and read the plan before you
  `--no-dry-run` the first time after upgrading.

- **`dp status` runs its checks concurrently**: 40.7s to 10.4s with five
  targets, one unreachable. Sections run in parallel and each database target is
  probed in parallel within them, which is where the time actually went — a 10s
  connect timeout per target was paid serially. Key order and section order are
  unchanged, because both pools iterate the declared mapping rather than
  completion order. The AWS section stays serial and last: it may hand the
  terminal to an interactive `aws sso login`.

### Added

- **`--verbose` (or `DP_VERBOSE=1`) shows what dataplat actually sent** — SQL
  statements, HTTP requests with status and duration, and AWS service calls.
  It writes to stderr only, so `--json` and `--format csv` stay pipeable, and
  everything passes through a redactor: passwords, secret values, bearer tokens
  and API keys are never traced. Parameter values and response bodies are not
  traced either — those are the data, not the request.

- **Third-party command areas.** `dp` discovers areas declared in the
  `dataplat.areas` entry-point group, so a package can add a command area
  without a change here. Discovery reads only the entry-point metadata, never
  imports the plugin, so `dp --version` and `dp --help` stay import-free and
  fast. A plugin that fails to import warns on stderr and leaves the built-in
  areas working; it cannot shadow a built-in area.

- `dp config doctor` warns when a loaded `.envrc` value still contains an
  unexpanded `$VAR`. dataplat does not run a shell, so
  `export PGHOST=$DB_HOST` loads the literal text — which then surfaces as a
  baffling connection failure rather than as the configuration mistake it is.

- Shell completion is documented (`dp --install-completion`). It always worked;
  the README never said so.

### Fixed

- `dp db query --format json|csv` could emit output that would not parse. The
  progress spinner painted to stdout, which was invisible while Rich only did
  that for a real terminal — the frames are erased — but `FORCE_COLOR` makes
  Rich treat a pipe as a terminal too, and then the escape sequences ended up in
  the redirected file. The spinner now follows the same sink as the notices.

- `dp db describe` reported the owner's grant option two different ways
  depending on whether you asked about a schema or a relation. PostgreSQL grants
  an owner every grant option implicitly and never records it in the ACL, so
  reading the ACL reported "cannot delegate" about a role that demonstrably can.
  Schema privileges now agree with relation privileges.

- The `Operating System :: OS Independent` classifier was an overclaim and has
  been narrowed. Four things break on Windows: `dp config init` creates a
  symlink, the dependency auto-install re-execs through `os.execvp`, the runner
  commands shell out to `docker` with a POSIX default workdir, and the
  credentials file is written with a `0o600` mode Windows ignores — after which
  dataplat reports its own file as insecurely permissioned. CI tests Linux only.

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
