"""Batch-job entrypoint for the monolith (Typer CLI).

This is a second entrypoint baked into the monolith image, distinct from the
API server entrypoint (``app/main.py``). It is built as ``:jobs_image`` from the
same source tree and dependency closure as the backend, so its image layers are
byte-identical to the backend image apart from the launcher: Bazel caches them
and the registry dedupes the blobs, making the jobs image nearly free to build
and push.

Each subcommand runs one batch job to completion and exits. These are the jobs
that previously ran inside the API pod via the in-process scheduler; running
them here keeps the API pod lean and lets the heavy work run in an ephemeral pod
(e.g. an Argo Workflow that invokes ``python app/jobs_main.py <command>`` against
this image).

To add a job: import its handler lazily inside the command function so module
import stays cheap and side-effect free, then dispatch to it.
"""

from __future__ import annotations

import asyncio
import logging
import os

import typer

from app.log import configure_logging

logger = logging.getLogger("monolith.jobs")

app = typer.Typer(
    help="Monolith batch jobs. Each command runs one job to completion and exits.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Run a single monolith batch job to completion and exit.

    The callback is intentionally empty: its presence forces Typer to keep the
    subcommand structure. Without it, a single-command app collapses into a
    bare command, so ``jobs_main.py worldcup-sim`` would reject ``worldcup-sim``
    as an unexpected argument.
    """


@app.command("worldcup-sim")
def worldcup_sim() -> None:
    """Run the World Cup 2026 qualification refresh as a one-shot.

    Polls worldcup26.ir, upserts standings and fixtures, runs the Monte Carlo
    qualification simulation, and persists qualification + swing-match rows.
    This is the one-shot form of the ``worldcup.refresh`` scheduled job; it
    opens its own session to mirror the scheduler's handler contract.
    """
    from sqlmodel import Session

    from app.db import get_engine
    from worldcup.jobs import refresh_handler

    configure_logging()
    logger.info("worldcup-sim: starting")
    with Session(get_engine()) as session:
        asyncio.run(refresh_handler(session))
    logger.info("worldcup-sim: done")


@app.command("test-agent")
def test_agent() -> None:
    """Substrate probe: query recent scheduler activity, ask Qwen to summarize it,
    and log the result. Zero side effects - this exists only to prove the
    Argo-CronWorkflow + on-cluster-Qwen path end to end (pod -> DB read -> Qwen
    inference -> output) before porting real agents off the bespoke orchestrator.
    """
    import httpx
    from sqlmodel import Session, text

    from app.db import get_engine

    configure_logging()
    logger.info("test-agent: starting")

    # 1. "Query logs": recent scheduler job activity (read-only, DATABASE_URL
    #    is already wired into the CronWorkflow).
    with Session(get_engine()) as session:
        rows = session.execute(
            text(
                "SELECT name, last_status, last_run_at FROM scheduler.scheduled_jobs "
                "ORDER BY last_run_at DESC NULLS LAST LIMIT 15"
            )
        ).fetchall()
    activity = "\n".join(f"- {r[0]}: {r[1]} @ {r[2]}" for r in rows) or "(no jobs)"
    logger.info("test-agent: read %d scheduler rows", len(rows))

    # 2. Ask the on-cluster Qwen (vLLM, OpenAI-compatible) to summarize.
    url = os.environ["LLAMA_CPP_URL"].rstrip("/")
    resp = httpx.post(
        f"{url}/v1/chat/completions",
        json={
            "model": "qwen3.6-27b",
            "messages": [
                {"role": "system", "content": "You are a terse SRE assistant."},
                {
                    "role": "user",
                    "content": "In one sentence, summarize this scheduler "
                    f"activity and flag anything failing:\n{activity}",
                },
            ],
            "max_tokens": int(os.environ.get("MAX_TOKENS", "200")),
            "temperature": 0,
            # qwen3.6 is a thinking model: left on, it spends the whole
            # max_tokens budget on <think> reasoning and returns content=null
            # (the reasoning lands in reasoning_content). Disable it so the
            # tokens produce the summary, matching chat/vision.py.
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=120,
    )
    resp.raise_for_status()
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        logger.error("test-agent: unexpected Qwen response shape: %s", exc)
        raise
    if not content:
        raise ValueError("test-agent: Qwen returned empty content")
    summary = content.strip()

    # 3. Log the result (the whole point of the probe).
    logger.info("test-agent: Qwen says: %s", summary)
    logger.info("test-agent: done")


if __name__ == "__main__":
    app()
