"""Scheduler CLI subcommands."""

from __future__ import annotations

import json

import typer

from tools.cli.api import request as _request
from tools.cli.output import scheduler_line

scheduler_app = typer.Typer(
    name="scheduler",
    help="Inspect and trigger scheduled jobs.",
    no_args_is_help=True,
)
jobs_app = typer.Typer(
    name="jobs",
    help="Inspect and trigger scheduled jobs.",
    no_args_is_help=True,
)
scheduler_app.add_typer(jobs_app)


def _print_job(job: dict) -> None:
    typer.echo(
        scheduler_line(
            name=job["name"],
            interval_secs=job["interval_secs"],
            next_run_at=job["next_run_at"],
            last_run_at=job.get("last_run_at"),
            last_status=job.get("last_status"),
        )
    )


@jobs_app.command("list")
def list_jobs(
    json_output: bool = typer.Option(False, "--json", help="Raw JSON output"),
) -> None:
    """List retained scheduled-job rows."""
    resp = _request("get", "/api/scheduler/jobs")
    resp.raise_for_status()
    data = resp.json()

    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    if not data:
        typer.echo("No scheduled job rows.")
        return

    for job in data:
        _print_job(job)


@jobs_app.command("get")
def get_job(
    name: str = typer.Argument(..., help="Job name (e.g. knowledge.gardener)"),
    json_output: bool = typer.Option(False, "--json", help="Raw JSON output"),
) -> None:
    """Show a single scheduled job by name."""
    resp = _request("get", f"/api/scheduler/jobs/{name}")
    if resp.status_code == 404:
        typer.echo(f"Unknown job: {name}", err=True)
        raise typer.Exit(code=1)
    resp.raise_for_status()
    job = resp.json()

    if json_output:
        typer.echo(json.dumps(job, indent=2))
        return

    _print_job(job)


@jobs_app.command("run-now")
def run_now(
    name: str = typer.Argument(..., help="Job whose Argo CronWorkflow should run once"),
    json_output: bool = typer.Option(False, "--json", help="Raw JSON output"),
) -> None:
    """Submit a job's Argo CronWorkflow as a one-off Workflow."""
    resp = _request("post", f"/api/scheduler/jobs/{name}/run-now")
    if resp.status_code == 404:
        typer.echo(f"Unknown job: {name}", err=True)
        raise typer.Exit(code=1)
    resp.raise_for_status()
    workflow = resp.json()

    if json_output:
        typer.echo(json.dumps(workflow, indent=2))
        return

    typer.echo(
        f"Submitted {workflow['workflow_name']} in {workflow['namespace']} for {name}."
    )
