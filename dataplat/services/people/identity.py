"""Who a person is, and what they are called on each system.

Every system here names the same person differently. One warehouse calls them
``bd_hlemm``, another ``h_lemm``, Superset ``hans.lemm`` -- three conventions
inside one company, and a different set at the next one. None of them belongs
in this tool: a convention hardcoded here is wrong for everyone else, and wrong
here too after the next rename.

So the convention is configuration. Each target declares a template in the
environment beside its connection settings, and this module turns an email
address into the username that template describes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from string import Formatter

from dataplat.core.errors import ValidationError

__all__ = [
    "PersonIdentity",
    "parse_identity",
    "render_username",
    "username_template",
]

_TEMPLATE_SUFFIX = "_USERNAME_TEMPLATE"


@dataclass(frozen=True)
class PersonIdentity:
    """One person, as much as an email address and the operator can say.

    ``first`` and ``last`` are ``None`` when nothing supplied them: an address
    like ``eva@`` says who the person is without saying what their last name
    is, and inventing one produces a plausible, wrong username.
    """

    email: str
    local: str
    first: str | None
    last: str | None


def parse_identity(
    email: str,
    first_name: str | None = None,
    last_name: str | None = None,
) -> PersonIdentity:
    """Read an identity out of an email address, with explicit names winning.

    The dotted local part is a convention, not a rule, so what it yields is
    only ever a default: ``--first-name``/``--last-name`` override it, and a
    local part that says nothing leaves the field empty rather than guessing.
    """
    if "@" not in email:
        raise ValidationError(f"{email!r} is not an email address.")

    local = email.rsplit("@", 1)[0].strip().lower()
    if not local:
        raise ValidationError(f"{email!r} has no name before the @.")

    parts = [part for part in local.split(".") if part]
    # The last segment only: "jan.van.der.berg" has no last name a username can
    # carry without a space in it.
    derived_first = parts[0] if parts else None
    derived_last = parts[-1] if len(parts) > 1 else None

    return PersonIdentity(
        email=email,
        local=local,
        first=first_name or derived_first,
        last=last_name or derived_last,
    )


def _fields(identity: PersonIdentity) -> dict[str, str | None]:
    return {
        "local": identity.local,
        "email": identity.email.lower(),
        "domain": identity.email.rsplit("@", 1)[-1].lower(),
        "first": identity.first,
        "last": identity.last,
        "first_initial": identity.first[0] if identity.first else None,
        "last_initial": identity.last[0] if identity.last else None,
    }


def render_username(template: str, identity: PersonIdentity) -> str:
    """Fill ``template`` for ``identity``, refusing rather than improvising.

    Parsed field by field instead of handed to ``str.format``: a bare format
    call reports a missing name as a ``KeyError`` naming the field and nothing
    else, and the operator needs to know which template, for which person, is
    unfillable -- that is the whole difference between a fixable message and a
    puzzle.
    """
    fields = _fields(identity)

    for _, field, _, _ in Formatter().parse(template):
        if field is None:
            continue
        if field not in fields:
            raise ValidationError(
                f"Unknown placeholder {{{field}}} in username template "
                f"{template!r}. Available: {', '.join(sorted(fields))}."
            )
        if fields[field] is None:
            raise ValidationError(
                f"Username template {template!r} needs {{{field}}}, which "
                f"{identity.email!r} does not supply. Pass --first-name/"
                "--last-name, or use a template that does not need it."
            )

    return template.format(**fields).lower()


def username_template(env_prefix: str, default: str | None = None) -> str | None:
    """The username convention configured for one target, if there is one.

    ``None`` is not a failure and not a missing default: it is how a target
    says people do not get accounts on it. An SSO-managed warehouse should
    never appear in an onboarding plan, and saying nothing is how it opts out.
    """
    raw = os.getenv(f"{env_prefix.upper()}{_TEMPLATE_SUFFIX}")
    if raw and raw.strip():
        return raw.strip()
    return default
