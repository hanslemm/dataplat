# Changelog

## Unreleased

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
