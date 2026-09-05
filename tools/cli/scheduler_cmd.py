"""Scheduler CLI subcommands."""

from __future__ import annotations

import json

import httpx
import typer

from tools.cli.auth import clear_cf_token, get_cf_token
from tools.cli.output import scheduler_line

API_BASE = "https://private.jomcgi.dev"

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


def _print_job(job: dict) -> None:
    typer.echo(
        scheduler_line(
            name=job["name"],
            interval_secs=job["interval_secs"],
            next_run_at=job["next_run_at"],
            last_run_at=job.get("last_run_at"),
            last_status=job.get("last_status"),
            has_handler=job.get("has_handler", True),
        )
    )


@jobs_app.command("list")
def list_jobs(
    json_output: bool = typer.Option(False, "--json", help="Raw JSON output"),
) -> None:
    """List every scheduled job."""
    resp = _request("get", "/api/scheduler/jobs")
    resp.raise_for_status()
    data = resp.json()

    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    if not data:
        typer.echo("No jobs registered.")
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
