"""CLI-level coverage for the Superset adapter.

The commands run against a fake Superset served through ``httpx.MockTransport``,
so the real service client, the real Typer wiring, and the real Rich render path
are all exercised — that is the only way a markup regression can be caught.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from dataplat.cli import _prompt
from dataplat.cli.bi import superset as superset_cli

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
    ) -> None:
        self.users = USERS if users is None else users
        self.roles = ROLES if roles is None else roles
        self.groups = GROUPS if groups is None else groups
        self.list_status = list_status
        self.list_reason = list_reason
        self.delete_status = delete_status
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


def test_missing_env_reports_the_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERSET_BASE_URL", raising=False)

    result = runner.invoke(superset_cli.app, ["roles", "list"])

    assert result.exit_code == 1
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

    assert result.exit_code == 1
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

    assert result.exit_code == 1
    assert "Unknown role(s): Nope" in result.output
    assert api.created == []


def test_user_update_requires_a_group_flag(api: FakeSuperset) -> None:
    result = runner.invoke(superset_cli.app, ["users", "update"])

    assert result.exit_code == 1
    assert "specify at least one group update flag" in result.output
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

    assert result.exit_code == 1
    assert "user 7:" in result.output
    assert "404" in result.output
