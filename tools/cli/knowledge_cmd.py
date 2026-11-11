"""Knowledge graph CLI subcommands."""

from __future__ import annotations

import json
from typing import Annotated, Optional

import typer

from tools.cli.api import request as _request
from tools.cli.output import compact_line, format_edges, search_line, write_to_tmpfile

knowledge_app = typer.Typer(
    name="knowledge",
    help="Search, read, and debug the knowledge graph.",
    no_args_is_help=True,
)


@knowledge_app.command()
def search(
    query: Annotated[str, typer.Argument(help="Natural language search query")],
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max results")] = 10,
    type: Annotated[
        Optional[str], typer.Option("--type", "-t", help="Filter by note type")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """Search the knowledge graph by natural language query."""
    params: dict = {"q": query, "limit": limit}
    if type:
        params["type"] = type

    resp = _request("get", "/api/knowledge/search", params=params)
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    results = data.get("results", [])
    if not results:
        typer.echo("No results.")
        return

    for r in results:
        typer.echo(
            search_line(
                r["score"],
                r["note_id"],
                r["title"],
                r.get("type", ""),
                r.get("edges", []),
            )
        )


@knowledge_app.command()
def note(
    note_id: Annotated[str, typer.Argument(help="Note ID to fetch")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """Fetch a note and write its content to a tmpfile."""
    resp = _request("get", f"/api/knowledge/notes/{note_id}")
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    tags = ", ".join(data.get("tags", []))
    edges = format_edges(data.get("edges", []))

    typer.echo(f"{data['title']} ({data.get('type', '')}) [{tags}]")
    if edges:
        typer.echo(f"Edges: {edges}")

    content = data.get("content", "")
    if content:
        path = write_to_tmpfile(note_id, content)
        typer.echo(f"Content: {path}")


@knowledge_app.command(name="dead-letters")
def dead_letters(
    json_output: Annotated[
        bool, typer.Option("--json", help="Raw JSON output")
    ] = False,
) -> None:
    """List raws that exhausted all retry attempts."""
    resp = _request("get", "/api/knowledge/dead-letter")
    resp.raise_for_status()

    data = resp.json()
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    items = data.get("items", [])
    if not items:
        typer.echo("No dead letters.")
        return

    for item in items:
        typer.echo(
            compact_line(
                item["id"],
                item["path"],
                item["source"],
                item.get("error"),
                item.get("retry_count", 0),
            )
        )


@knowledge_app.command()
def replay(
    raw_id: Annotated[int, typer.Argument(help="Raw ID to replay")],
) -> None:
    """Replay a dead-lettered raw so the gardener retries it."""
    resp = _request("post", f"/api/knowledge/dead-letter/{raw_id}/replay")
    if resp.status_code == 404:
        typer.echo(f"Raw {raw_id} not found or not dead-lettered.", err=True)
        raise typer.Exit(1)

    resp.raise_for_status()
    typer.echo(f"Replayed raw {raw_id}. It will be retried on the next gardener cycle.")


from tools.cli.tasks_cmd import tasks_app  # noqa: E402

knowledge_app.add_typer(tasks_app)
