"""Task tracking CLI subcommands."""

from __future__ import annotations

import json
from typing import Annotated, Optional

import httpx
import typer

from tools.cli.auth import clear_cf_token, get_cf_token
from tools.cli.output import task_line

API_BASE = "https://private.jomcgi.dev"

tasks_app = typer.Typer(
    name="tasks",
    help="List, search, and manage knowledge-graph tasks.",
    invoke_without_command=True,
)


def _client() -> httpx.Client:
    token = get_cf_token()
    return httpx.Client(
        base_url=API_BASE,
        cookies={"CF_Authorization": token},
        follow_redirects=False,
        timeout=30.0,
    )


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Make a request with automatic re-auth on 3xx (expired CF token)."""
    with _client() as client:
        resp = getattr(client, method)(path, **kwargs)

    if resp.is_redirect:
        typer.echo("Token expired, re-authenticating...", err=True)
        clear_cf_token()
        with _client() as client:
            resp = getattr(client, method)(path, **kwargs)

    return resp


def _print_tasks(tasks: list[dict]) -> None:
    """Print a list of task dicts using task_line formatter."""
    if not tasks:
        typer.echo("No tasks.")
        return
    for t in tasks:
        typer.echo(
            task_line(
                t["note_id"],
                t["title"],
                t["status"],
                size=t.get("size"),
                due=t.get("due"),
                blocked_by=t.get("blocked_by"),
            )
        )


@tasks_app.callback()
def list_tasks(
    ctx: typer.Context,
    status: Annotated[
        Optional[str], typer.Option("--status", "-s", help="Filter by status")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """List tasks (default when no subcommand given)."""
    if ctx.invoked_subcommand is not None:
        return

    params: dict = {}
    if status:
        params["status"] = status

    resp = _request("get", "/api/knowledge/tasks", params=params)
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    _print_tasks(data.get("tasks", []))


@tasks_app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """Search tasks by keyword or natural language."""
    resp = _request("get", "/api/knowledge/tasks", params={"q": query})
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    _print_tasks(data.get("tasks", []))


@tasks_app.command()
def done(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Mark a task as done."""
    resp = _request("patch", f"/api/knowledge/tasks/{note_id}", json={"status": "done"})
    resp.raise_for_status()
    typer.echo(f"Marked {note_id} as done.")


@tasks_app.command()
def cancel(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Cancel a task."""
    resp = _request(
        "patch", f"/api/knowledge/tasks/{note_id}", json={"status": "cancelled"}
    )
    resp.raise_for_status()
    typer.echo(f"Cancelled {note_id}.")


@tasks_app.command()
def block(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Mark a task as blocked."""
    resp = _request(
        "patch", f"/api/knowledge/tasks/{note_id}", json={"status": "blocked"}
    )
    resp.raise_for_status()
    typer.echo(f"Marked {note_id} as blocked.")


@tasks_app.command()
def activate(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Mark a task as active."""
    resp = _request(
        "patch", f"/api/knowledge/tasks/{note_id}", json={"status": "active"}
    )
    resp.raise_for_status()
    typer.echo(f"Marked {note_id} as active.")


@tasks_app.command()
def add(
    title: Annotated[str, typer.Argument(help="Title of the new task")],
    due: Annotated[
        str | None, typer.Option("--due", help="Due date (YYYY-MM-DD)")
    ] = None,
    status: Annotated[
        str,
        typer.Option("--status", help="todo, in-progress, done, cancelled, or someday"),
    ] = "todo",
) -> None:
    """Create a task and print its note ID."""
    body = {"title": title, "status": status}
    if due is not None:
        body["due"] = due
    resp = _request("post", "/api/knowledge/tasks", json=body)
    resp.raise_for_status()
    typer.echo(resp.json()["note_id"])


@tasks_app.command()
def daily(
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """Show daily task summary."""
    resp = _request("get", "/api/knowledge/tasks/daily")
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    _print_tasks(data.get("tasks", []))


@tasks_app.command()
def weekly(
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """Show weekly task summary."""
    resp = _request("get", "/api/knowledge/tasks/weekly")
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    _print_tasks(data.get("tasks", []))
