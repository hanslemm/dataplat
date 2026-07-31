"""Typer options and the protected-schema rule shared by ``dp db schema``.

In their own module so ``schema.py`` can import the command modules while the
command modules import these — a constant living in ``schema.py`` would be a
cycle. No logic beyond the refusal predicate, and nothing here imports a command.
"""

from __future__ import annotations

import typer

__all__ = [
    "DefaultForOption",
    "PrivilegesOption",
    "SchemaLikeOption",
    "SchemaSelectOption",
    "ToKindOption",
    "is_protected_schema",
]

SchemaSelectOption = typer.Option(
    None, "--schemas", help="Target schemas. Repeatable / comma-separated."
)
SchemaLikeOption = typer.Option(
    None,
    "--like",
    help="Target schemas by pattern instead of naming them; glob `*` works as "
    "SQL `%` (e.g. dev_*).",
)
PrivilegesOption = typer.Option(
    None,
    "--privileges",
    "-p",
    help="Privileges or presets. Repeatable / comma-separated. "
    "Presets: read, readwrite.",
)
ToKindOption = typer.Option(
    None,
    "--to-kind",
    help="Disambiguate a grantee name that exists as more than one object.",
)
DefaultForOption = typer.Option(
    None,
    "--default-for",
    help="Grantor for default-* privileges (ALTER DEFAULT PRIVILEGES FOR ...). "
    "Defaults to each schema's own owner.",
)

#: Schemas the CLI refuses to touch destructively, whatever the operator typed.
#: Shared by ``schema drop`` (refusing to drop) and ``schema alter`` (refusing to
#: reown, requota or rename).
#:
#: ``public`` and ``main`` are each their engine's *default* schema rather than a
#: catalog — which is exactly why they are listed. They are the schemas most
#: likely to be swept up by a careless ``--like``, and dropping either one breaks
#: every unqualified reference in the database. ``catalog_history`` is Redshift's.
_PROTECTED_SCHEMA_NAMES = frozenset(
    {"information_schema", "public", "main", "catalog_history"}
)


def is_protected_schema(name: str) -> bool:
    """Whether ``name`` must never be dropped or altered by this CLI.

    ``casefold()`` first, so the refusal is correct on its own rather than
    correct only because callers happen to pass lowercase names — a
    ``--like 'PUBLIC'`` that slipped through would otherwise be quoted downstream
    and merely fail to find a schema, which looks like success.
    """
    folded = name.casefold()
    return folded.startswith("pg_") or folded in _PROTECTED_SCHEMA_NAMES
