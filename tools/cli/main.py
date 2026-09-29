"""Homelab CLI — token-efficient cluster tooling for Claude Code."""

from __future__ import annotations

import typer

from tools.cli.knowledge_cmd import knowledge_app
from tools.cli.pr_cmd import pr_app
from tools.cli.scheduler_cmd import scheduler_app

app = typer.Typer(
    name="homelab",
    help="Token-efficient CLI for homelab operations.",
    no_args_is_help=True,
)

app.add_typer(knowledge_app)
app.add_typer(pr_app)
app.add_typer(scheduler_app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
