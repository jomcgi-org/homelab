"""Submit monolith batch jobs to Argo Workflows (off-pod execution).

The monolith stays the orchestrator; Argo is a stateless executor that runs the
jobs image to completion in the unmeshed ``monolith-workflows`` namespace. This
module is gated by the ``JOB_EXECUTOR`` env var:

- ``monolith`` (default): jobs run in-process in the API pod, as before.
- ``argo``: the scheduler submits a Workflow instead, and the work runs in an
  ephemeral pod invoking the jobs Typer image.

The split lets us deploy and exercise the Argo path while the in-process path
stays live, then perform the cutover by flipping a single env var.

The required job env (e.g. ``DATABASE_URL``) is read from THIS process's
environment - the API pod already holds the resolved secrets - and injected as
literal values into the Workflow's container, so the workflow namespace needs no
copy of those secrets.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("monolith.scheduler.argo")

_DEFAULT_WORKFLOW_NAMESPACE = "monolith-workflows"
# The ServiceAccount the chart creates for Workflow pods (reports status back to
# the controller). See projects/platform/argo-workflows values.workflow.serviceAccount.
_WORKFLOW_SERVICE_ACCOUNT = "argo-workflow"


def jobs_use_argo() -> bool:
    """True when batch jobs should be submitted to Argo Workflows."""
    return os.environ.get("JOB_EXECUTOR", "monolith").strip().lower() == "argo"


def workflow_namespace() -> str:
    return os.environ.get("WORKFLOW_NAMESPACE", _DEFAULT_WORKFLOW_NAMESPACE)


def build_job_workflow(name: str, args: list[str], env_keys: list[str]) -> dict:
    """Build an Argo Workflow manifest (dict) that runs the jobs image once.

    The container uses the jobs image's own entrypoint (the Typer CLI) and passes
    ``args`` as the subcommand (e.g. ``["worldcup-sim"]``), so no command override
    and no assumption about ``python`` being on PATH. Only env keys that are
    actually set in this process are forwarded.
    """
    from hera.workflows import Container, Env, Workflow
    from hera.workflows.models import IntOrString, RetryStrategy, TTLStrategy

    image = os.environ["JOBS_IMAGE"]
    env = [Env(name=k, value=os.environ[k]) for k in env_keys if os.environ.get(k)]

    with Workflow(
        api_version="argoproj.io/v1alpha1",
        kind="Workflow",
        generate_name=f"{name}-",
        namespace=workflow_namespace(),
        entrypoint="run",
        service_account_name=_WORKFLOW_SERVICE_ACCOUNT,
        # GC finished workflows: keep successes briefly, failures longer to debug.
        ttl_strategy=TTLStrategy(
            seconds_after_completion=3600,
            seconds_after_success=3600,
            seconds_after_failure=86400,
        ),
    ) as w:
        Container(
            name="run",
            image=image,
            args=args,
            env=env,
            # OnError only: retry pure-infra failures (spot eviction, OOM, node
            # death) in-cluster, but let logic/exit!=0 failures bubble up so the
            # monolith re-submits with current code rather than replaying a stale
            # spec. Argo's native retry would replay the same image+args.
            retry_strategy=RetryStrategy(
                limit=IntOrString(root="2"),
                retry_policy="OnError",
            ),
        )
    return w.to_dict()


async def submit_job_workflow(name: str, args: list[str], env_keys: list[str]) -> str:
    """Submit a job Workflow to the workflow namespace; return its assigned name."""
    from cluster.api import KubernetesClient

    body = build_job_workflow(name, args, env_keys)
    created = await KubernetesClient().create_workflow(workflow_namespace(), body)
    logger.info("submitted Argo workflow %s for job %s", created, name)
    return created
