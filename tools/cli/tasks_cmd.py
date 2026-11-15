"""Task tracking CLI subcommands."""

from __future__ import annotations

import json
from typing import Annotated, Optional

import typer

from tools.cli.api import request as _request
from tools.cli.output import task_line

tasks_app = typer.Typer(
    name="tasks",
    help="List, search, and manage knowledge-graph tasks.",
    invoke_without_command=True,
)


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


def _show_tasks(path: str, json_output: bool, **kwargs) -> None:
    """GET *path* and print its tasks as JSON or one line each."""
    resp = _request("get", path, **kwargs)
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    _print_tasks(data.get("tasks", []))


def _set_status(note_id: str, status: str, message: str) -> None:
    resp = _request("patch", f"/api/knowledge/tasks/{note_id}", json={"status": status})
    resp.raise_for_status()
    typer.echo(message)


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

    _show_tasks("/api/knowledge/tasks", json_output, params=params)


@tasks_app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """Search tasks by keyword or natural language."""
    _show_tasks("/api/knowledge/tasks", json_output, params={"q": query})


@tasks_app.command()
def done(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Mark a task as done."""
    _set_status(note_id, "done", f"Marked {note_id} as done.")


@tasks_app.command()
def cancel(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Cancel a task."""
    _set_status(note_id, "cancelled", f"Cancelled {note_id}.")


@tasks_app.command()
def block(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Mark a task as blocked."""
    _set_status(note_id, "blocked", f"Marked {note_id} as blocked.")


@tasks_app.command()
def activate(
    note_id: Annotated[str, typer.Argument(help="Note ID of the task")],
) -> None:
    """Mark a task as active."""
    _set_status(note_id, "active", f"Marked {note_id} as active.")


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
    _show_tasks("/api/knowledge/tasks/daily", json_output)


@tasks_app.command()
def weekly(
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """Show weekly task summary."""
    _show_tasks("/api/knowledge/tasks/weekly", json_output)
