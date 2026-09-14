"""CLI-level coverage for the Superset adapter.

The commands run against a fake Superset served through ``httpx.MockTransport``,
so the real service client, the real Typer wiring, and the real Rich render path
are all exercised — that is the only way a markup regression can be caught.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from dataplat.cli import _credentials, _prompt
from dataplat.cli.bi import superset as superset_cli
from dataplat.core.errors import ExitCode

runner = CliRunner()

# Rich wraps table cells at the console width; 80 columns would fold the values
# the assertions look for.
WIDE = {"COLUMNS": "200"}

ROLES: list[dict[str, Any]] = [
    {"id": 1, "name": "Admin"},
    {"id": 2, "name": "Gamma"},
]

GROUPS: list[dict[str, Any]] = [
    {"id": 10, "name": "analysts", "label": "Analysts", "description": "SQL folks"},
    {"id": 11, "name": "viewers", "label": None, "description": None},
]

USERS: list[dict[str, Any]] = [
    {
        "id": 7,
        "username": "ada",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "active": True,
        "roles": [{"id": 2, "name": "Gamma"}],
        "groups": [],
    },
    {
        "id": 8,
        "username": "bob",
        "first_name": "Bob",
        "last_name": "Bobson",
        "email": "bob@example.com",
        "active": False,
        "roles": [{"id": 1, "name": "Admin"}],
        "groups": [{"id": 11, "name": "viewers"}],
    },
]


class FakeSuperset:
    """The slice of the Superset security API the CLI actually calls."""

    def __init__(
        self,
        *,
        users: list[dict[str, Any]] | None = None,
        roles: list[dict[str, Any]] | None = None,
        groups: list[dict[str, Any]] | None = None,
        list_status: int = 200,
        list_reason: str = "",
        delete_status: int = 200,
        create_status: int = 201,
        create_body: dict[str, Any] | None = None,
    ) -> None:
        self.users = USERS if users is None else users
        self.roles = ROLES if roles is None else roles
        self.groups = GROUPS if groups is None else groups
        self.list_status = list_status
        self.list_reason = list_reason
        self.delete_status = delete_status
        self.create_status = create_status
        self.create_body = create_body
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []

    def _page(self, items: list[dict[str, Any]]) -> httpx.Response:
        if self.list_status >= 400:
            return httpx.Response(
                self.list_status,
                json={"message": "nope"},
                extensions={"reason_phrase": self.list_reason.encode()},
            )
        return httpx.Response(200, json={"result": items, "count": len(items)})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "POST" and path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"})
        if method == "GET" and path.endswith("/security/roles/"):
            return self._page(self.roles)
        if method == "GET" and path.endswith("/security/groups/"):
            return self._page(self.groups)
        if method == "GET" and path.endswith("/security/users/"):
            return self._page(self.users)
        if method == "POST" and path.endswith("/security/users/"):
            if self.create_status >= 400:
                # httpx derives the reason phrase from the status, so the
                # rendered error is the one Superset would produce.
                return httpx.Response(self.create_status, json=self.create_body or {})
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 99})
        if method == "PUT" and "/security/users/" in path:
            self.updated.append((path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={})
        if method == "DELETE" and "/security/users/" in path:
            user_id = path.rsplit("/", 1)[-1]
            if self.delete_status >= 400:
                return httpx.Response(
                    self.delete_status, extensions={"reason_phrase": b"Not Found"}
                )
            self.deleted.append(user_id)
            return httpx.Response(200, json={"message": "OK"})
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.fixture(autouse=True)
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERSET_BASE_URL", "https://superset.test")
    monkeypatch.setenv("SUPERSET_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_ADMIN_PASSWORD", "secret")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSuperset]:
    """Serve the fake API to every ``httpx.Client()`` the CLI opens."""
    fake = FakeSuperset()
    _serve(monkeypatch, fake)
    yield fake


def _serve(monkeypatch: pytest.MonkeyPatch, fake: FakeSuperset) -> None:
    transport = httpx.MockTransport(fake.handler)
    # Bind the real constructor first: the patched name is looked up again on
    # every call, so a lambda calling httpx.Client() would recurse forever.
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real_client(transport=transport),
    )


def _tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the confirmation gate treat the session as interactive.

    Only ``_prompt``'s view of stdin is swapped; CliRunner keeps serving the
    ``input=`` text to the prompt itself.
    """
    monkeypatch.setattr(
        _prompt, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )


@pytest.mark.parametrize(
    "command",
    [
        ["roles", "list"],
        ["users", "create", "newbie", "--password", "pw", "--email", "n@example.com"],
        ["users", "update", "--add-group", "analysts"],
        ["users", "list"],
        ["users", "delete", "7", "-y"],
        ["groups", "list"],
    ],
    ids=lambda c: " ".join(c[:2]),
)
def test_every_command_opens_the_traced_client(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """No command may build its own ``httpx.Client``.

    ``build_client`` is where the ``--verbose`` event hooks live, so a command
    that calls ``httpx.Client()`` directly still works and silently traces
    nothing — the one failure mode an opt-in diagnostic has. Every command is
    listed because a single missed site is exactly what would slip through.
    Deliberately does not patch ``httpx.Client``: with the factory bypassed the
    spy is never called, and this fails instead of quietly passing.
    """
    transport = httpx.MockTransport(FakeSuperset().handler)
    opened: list[bool] = []

    def spy() -> httpx.Client:
        opened.append(True)
        return httpx.Client(transport=transport)

    monkeypatch.setattr(superset_cli, "build_client", spy)

    result = runner.invoke(superset_cli.app, command, env=WIDE)

    assert result.exit_code == 0, result.output
    assert opened == [True]


def test_missing_env_reports_the_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERSET_BASE_URL", raising=False)

    result = runner.invoke(superset_cli.app, ["roles", "list"])

    assert result.exit_code == ExitCode.CONFIG
    assert "SUPERSET_BASE_URL" in result.output


def test_roles_list_renders_table(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["roles", "list"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Admin" in result.output
    assert "Gamma" in result.output


def test_roles_list_orders_by_id_descending(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["roles", "list", "--order", "id", "--order-dir", "desc"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert result.output.index("Gamma") < result.output.index("Admin")


def test_roles_list_json_is_untouched(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["roles", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert result.output == json.dumps(ROLES, indent=2, ensure_ascii=False) + "\n"


def test_roles_list_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, FakeSuperset(roles=[]))

    result = runner.invoke(superset_cli.app, ["roles", "list"])

    assert result.exit_code == 0, result.output
    assert "No roles found" in result.output


def test_users_list_renders_table(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["users", "list"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "ada@example.com" in result.output
    assert "Bob Bobson" in result.output
    assert "Total: 2 user(s)" in result.output


def test_users_list_filters_by_role(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app, ["users", "list", "--filter-role", "Admin"], env=WIDE
    )

    assert result.exit_code == 0, result.output
    assert "bob" in result.output
    assert "ada" not in result.output
    assert "Total: 1 user(s)" in result.output


def test_users_list_json_is_untouched(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["users", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert result.output == json.dumps(USERS, indent=2, ensure_ascii=False) + "\n"


def test_groups_list_renders_table(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["groups", "list"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "analysts" in result.output
    assert "SQL folks" in result.output
    assert "Total: 2 group(s)" in result.output


def test_groups_list_json_is_untouched(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["groups", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert result.output == json.dumps(GROUPS, indent=2, ensure_ascii=False) + "\n"


def test_api_error_body_is_shown_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service error carrying markup must be readable, not a traceback."""
    _serve(
        monkeypatch,
        FakeSuperset(list_status=500, list_reason="Boom [/issue] [bold]"),
    )

    result = runner.invoke(superset_cli.app, ["groups", "list"], env=WIDE)

    assert result.exit_code == ExitCode.SERVICE
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Boom [/issue] [bold]" in result.output


def test_hostile_values_render_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose values look like markup used to crash or vanish.

    ``[/issue]`` raised MarkupError mid-render; ``[bold]`` was swallowed and the
    table lied about the data. Both must now appear verbatim.
    """
    hostile = [
        {
            "id": 3,
            "username": "closes [/issue] 42",
            "first_name": "[bold]",
            "last_name": "",
            "email": "hostile@example.com",
            "active": True,
            "roles": [{"id": 2, "name": "role [/x]"}],
            "groups": [],
        }
    ]
    _serve(monkeypatch, FakeSuperset(users=hostile))

    result = runner.invoke(superset_cli.app, ["users", "list"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "closes [/issue] 42" in result.output
    assert "[bold]" in result.output
    assert "role [/x]" in result.output


def test_hostile_values_stay_raw_in_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escaping is a render-time concern: --json must stay byte-identical."""
    hostile = [{"id": 4, "username": "a [/x] b", "roles": [], "groups": []}]
    _serve(monkeypatch, FakeSuperset(users=hostile))

    result = runner.invoke(superset_cli.app, ["users", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert result.output == json.dumps(hostile, indent=2, ensure_ascii=False) + "\n"


def test_user_create_posts_resolved_payload(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        [
            "users",
            "create",
            "newbie",
            "--password",
            "pw",
            "--email",
            "newbie@example.com",
            "--group",
            "analysts",
        ],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.created == [
        {
            "username": "newbie",
            "first_name": "newbie",
            "last_name": "User",
            "email": "newbie@example.com",
            "password": "pw",
            "active": True,
            "roles": [2],
            "groups": [10],
        }
    ]
    assert "id=99" in result.output


def test_user_create_prompts_for_password_without_echo(api: FakeSuperset) -> None:
    """The password is a secret: it is prompted, confirmed, and never echoed."""
    result = runner.invoke(
        superset_cli.app,
        ["users", "create", "quiet", "--email", "quiet@example.com"],
        input="s3cret\ns3cret\n",
    )

    assert result.exit_code == 0, result.output
    assert api.created[0]["password"] == "s3cret"
    assert "s3cret" not in result.output


def test_user_create_reports_the_reason_superset_gave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected create must arrive with its cause attached.

    "422 Unprocessable Entity" alone is true of a duplicate username, a refused
    password and a field this client spelled wrong alike; only the body tells
    the operator which one they hit.
    """
    _serve(
        monkeypatch,
        FakeSuperset(
            create_status=422,
            create_body={"message": {"username": ["Already exists"]}},
        ),
    )

    result = runner.invoke(
        superset_cli.app,
        ["users", "create", "newbie", "--password", "pw", "--email", "n@example.com"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SERVICE
    assert "422" in result.output
    assert "username: Already exists" in result.output


def test_user_create_refuses_a_username_that_is_taken(api: FakeSuperset) -> None:
    """The duplicate is named before the POST, not decoded from a 422 after it."""
    result = runner.invoke(
        superset_cli.app,
        ["users", "create", "ADA", "--password", "pw", "--email", "new@example.com"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "already has a user with that username" in result.output
    assert "id=7" in result.output
    assert api.created == []


def test_user_create_refuses_an_email_that_is_taken(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "create", "fresh", "--password", "pw", "--email", "Ada@Example.com"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "already has a user with that email" in result.output
    assert "username=ada" in result.output
    assert api.created == []


def test_user_create_derives_username_and_names_from_the_email(
    api: FakeSuperset,
) -> None:
    """``-U`` exists so the local part is never typed twice."""
    result = runner.invoke(
        superset_cli.app,
        [
            "users",
            "create",
            "--password",
            "pw",
            "--email",
            "Eva.Germeshausen@betterdoc.de",
            "-U",
        ],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.created == [
        {
            "username": "eva.germeshausen",
            "first_name": "Eva",
            "last_name": "Germeshausen",
            "email": "Eva.Germeshausen@betterdoc.de",
            "password": "pw",
            "active": True,
            "roles": [2],
        }
    ]


def test_derived_username_without_a_dot_keeps_the_name_defaults(
    api: FakeSuperset,
) -> None:
    """One name part is a username, not a first and last name."""
    result = runner.invoke(
        superset_cli.app,
        ["users", "create", "--password", "pw", "--email", "eva@betterdoc.de", "-U"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.created[0]["username"] == "eva"
    assert api.created[0]["first_name"] == "eva"
    assert api.created[0]["last_name"] == "User"


def test_explicit_names_survive_the_derivation(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        [
            "users",
            "create",
            "--password",
            "pw",
            "--email",
            "eva.germeshausen@betterdoc.de",
            "--derive-username",
            "--first-name",
            "Eva-Maria",
        ],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.created[0]["first_name"] == "Eva-Maria"
    assert api.created[0]["last_name"] == "Germeshausen"


def test_user_create_needs_a_username_or_the_derive_flag(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "create", "--password", "pw", "--email", "eva@betterdoc.de"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "--derive-username" in result.output
    assert api.created == []


def test_user_create_rejects_a_username_next_to_the_derive_flag(
    api: FakeSuperset,
) -> None:
    """Two sources for one value would make the losing one silent."""
    result = runner.invoke(
        superset_cli.app,
        [
            "users",
            "create",
            "eva",
            "--password",
            "pw",
            "--email",
            "eva@betterdoc.de",
            "-U",
        ],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "not both" in result.output
    assert api.created == []


def test_derivation_rejects_an_email_with_no_local_part(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "create", "--password", "pw", "--email", "@betterdoc.de", "-U"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "Cannot derive a username" in result.output
    assert api.created == []


def test_user_create_unknown_role_is_reported(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        [
            "users",
            "create",
            "newbie",
            "--password",
            "pw",
            "--email",
            "n@example.com",
            "--role",
            "Nope",
        ],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.CONFIG
    assert "Unknown role(s): Nope" in result.output
    assert api.created == []


def test_user_update_requires_something_to_change(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["users", "update"])

    assert result.exit_code == 1
    assert "specify at least one group or role update flag" in result.output
    assert api.updated == []


def test_user_update_rejects_set_group_with_add_group(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--set-group", "viewers", "--add-group", "analysts"],
        env=WIDE,
    )

    assert result.exit_code == 1
    assert "cannot be combined" in result.output
    assert api.updated == []


def test_user_update_adds_group(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--user-id", "7", "--add-group", "analysts"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated == [("7", {"roles": [2], "groups": [10]})]
    assert "Updated 1 user(s)" in result.output


def test_user_update_dry_run_changes_nothing(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--add-group", "analysts", "--dry-run"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated == []
    assert "dry run" in result.output


def test_user_update_no_match(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--user-id", "9999", "--add-group", "analysts"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated == []
    assert "No users matched" in result.output


def test_user_delete_confirmed_interactively(
    api: FakeSuperset, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tty(monkeypatch)

    result = runner.invoke(superset_cli.app, ["users", "delete", "7"], input="y\n")

    assert result.exit_code == 0, result.output
    assert api.deleted == ["7"]
    assert "Delete Superset user(s) 7?" in result.output


def test_user_delete_declined_deletes_nothing(
    api: FakeSuperset, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tty(monkeypatch)

    result = runner.invoke(superset_cli.app, ["users", "delete", "7"], input="n\n")

    assert result.exit_code == 1
    assert api.deleted == []
    assert "Aborted." in result.output


def test_user_delete_non_interactive_names_the_flag(api: FakeSuperset) -> None:
    """A pipe must never block, and must say what would have worked."""
    result = runner.invoke(superset_cli.app, ["users", "delete", "7", "8"], env=WIDE)

    assert result.exit_code == 1
    assert api.deleted == []
    assert "--yes" in result.output


def test_user_delete_with_yes_skips_the_prompt(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["users", "delete", "7", "8", "--yes"])

    assert result.exit_code == 0, result.output
    assert api.deleted == ["7", "8"]
    assert "Delete Superset user(s)" not in result.output


def test_user_delete_reports_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, FakeSuperset(delete_status=404))

    result = runner.invoke(superset_cli.app, ["users", "delete", "7", "-y"], env=WIDE)

    assert result.exit_code == ExitCode.SERVICE
    assert "user 7:" in result.output
    assert "404" in result.output


def test_user_delete_keeps_going_after_a_failed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad id must not abandon the rest.

    The exit code is now read off the errors the loop collected, so this is what
    keeps that from quietly becoming "stop at the first failure": every id is
    still attempted, and the code still says SERVICE rather than 1.
    """
    _serve(monkeypatch, FakeSuperset(delete_status=404))

    result = runner.invoke(
        superset_cli.app, ["users", "delete", "7", "8", "-y"], env=WIDE
    )

    assert result.exit_code == ExitCode.SERVICE
    assert "user 7:" in result.output
    assert "user 8:" in result.output


# ---------------------------------------------------------------------------
# users set-password
# ---------------------------------------------------------------------------
# Superset has no password reset in its own UI for an admin acting on someone
# else, and `users update` here only moves groups -- so an account whose
# password is lost had nowhere to go but delete-and-recreate.


def _creds_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(_credentials, "CREDENTIALS_DIR", tmp_path / "credentials")


def test_set_password_puts_only_the_password_for_the_named_user(
    api: FakeSuperset,
) -> None:
    """Everything else about the account must survive a password change."""
    result = runner.invoke(
        superset_cli.app,
        ["users", "set-password", "ada", "--password", "n3w"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated == [("7", {"password": "n3w"})]


def test_set_password_matches_the_username_case_insensitively(
    api: FakeSuperset,
) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "set-password", "ADA", "--password", "n3w"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated == [("7", {"password": "n3w"})]


def test_set_password_rejects_an_unknown_username(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "set-password", "nobody", "--password", "n3w"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "no Superset user" in result.output
    assert api.updated == []


def test_set_password_prompts_without_echo(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "set-password", "ada"],
        input="s3cret\ns3cret\n",
    )

    assert result.exit_code == 0, result.output
    assert api.updated[0][1]["password"] == "s3cret"
    assert "s3cret" not in result.output


def test_generated_password_is_recorded_and_never_printed(
    api: FakeSuperset, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The password reaches a 0600 file, not the scrollback of a shared screen."""
    _creds_dir(monkeypatch, tmp_path)

    result = runner.invoke(
        superset_cli.app,
        ["users", "set-password", "ada", "--generate-password"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    sent = api.updated[0][1]["password"]
    assert len(sent) == 32
    assert sent not in result.output

    written = list((tmp_path / "credentials").glob("dp-superset-credentials-*.csv"))
    assert len(written) == 1
    rows = list(csv.reader(written[0].read_text().splitlines()))
    assert rows[0] == ["username", "password", "created_at", "superset_url"]
    assert rows[1][0] == "ada"
    assert rows[1][1] == sent
    assert rows[1][3] == "https://superset.test"
    assert written[0].stat().st_mode & 0o077 == 0


def test_set_password_rejects_a_password_next_to_generate(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "set-password", "ada", "--password", "n3w", "--generate-password"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "not both" in result.output
    assert api.updated == []


def test_user_create_can_generate_its_password(
    api: FakeSuperset, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The same generator, so a new account never needs a password invented."""
    _creds_dir(monkeypatch, tmp_path)

    result = runner.invoke(
        superset_cli.app,
        [
            "users",
            "create",
            "--email",
            "eva.germeshausen@betterdoc.de",
            "-U",
            "-G",
        ],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    sent = api.created[0]["password"]
    assert len(sent) == 32
    assert sent not in result.output

    written = list((tmp_path / "credentials").glob("dp-superset-credentials-*.csv"))
    rows = list(csv.reader(written[0].read_text().splitlines()))
    assert rows[1][0] == "eva.germeshausen"
    assert rows[1][1] == sent


# ---------------------------------------------------------------------------
# users update: roles, not just groups
# ---------------------------------------------------------------------------
# Roles are Superset's privilege tiers, and until now nothing here could change
# one: `update` moved groups, `roles list` listed. Revoking someone's Admin
# meant the API by hand.


def test_a_role_can_be_added(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--email", "ada@example.com", "--add-role", "Admin"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    user_id, payload = api.updated[0]
    assert user_id == "7"
    assert sorted(payload["roles"]) == [1, 2]  # Admin added beside Gamma


def test_a_role_can_be_removed(api: FakeSuperset) -> None:
    """The thing that could not be done at all before."""
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--email", "bob@example.com", "--remove-role", "Admin"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    user_id, payload = api.updated[0]
    assert user_id == "8"
    assert payload["roles"] == []


def test_roles_can_be_replaced_wholesale(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--email", "bob@example.com", "--set-role", "Gamma"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated[0][1]["roles"] == [2]


def test_a_role_change_alone_is_still_a_change(api: FakeSuperset) -> None:
    """The skip test used to compare groups only.

    A user whose groups stay put but whose roles change would have been
    skipped as "nothing to do" -- which is the whole of this feature.
    """
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--email", "ada@example.com", "--add-role", "Admin"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated, "a role-only change was skipped"
    assert "Updated 1" in result.output


def test_groups_survive_a_role_change(api: FakeSuperset) -> None:
    """The payload carries both, so neither may be dropped by touching the other."""
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--email", "bob@example.com", "--remove-role", "Admin"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated[0][1]["groups"] == [11]  # bob's existing group, untouched


def test_set_role_cannot_be_combined_with_add_role(api: FakeSuperset) -> None:
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--set-role", "Gamma", "--add-role", "Admin"],
        env=WIDE,
    )

    assert result.exit_code != 0
    assert api.updated == []


def test_update_still_needs_something_to_do(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["users", "update"], env=WIDE)

    assert result.exit_code != 0
    assert "role" in result.output
    assert api.updated == []


def test_an_unchanged_role_set_is_not_rewritten(api: FakeSuperset) -> None:
    """Adding a role someone already holds is not a write."""
    result = runner.invoke(
        superset_cli.app,
        ["users", "update", "--email", "ada@example.com", "--add-role", "Gamma"],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert api.updated == []
