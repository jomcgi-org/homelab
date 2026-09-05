"""Read operations and Argo Workflow submission for the scheduler API."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os

from kubernetes_asyncio.client.exceptions import ApiException
from sqlmodel import Session, select

from cluster.kubernetes import KubernetesClient
from scheduler.api import ScheduledJob, is_registered
from scheduler.views import SchedulerJobView

_REPLACES_ANNOTATION = "monolith.jomcgi.dev/replaces"
_CRON_WORKFLOW_LABEL = "workflows.argoproj.io/cron-workflow"


@dataclass(frozen=True)
class RunNowResult:
    """Outcome of submitting a scheduler job as an Argo Workflow."""

    job: str
    workflow_name: str | None
    namespace: str
    status_code: int
    message: str | None = None


def _to_view(job: ScheduledJob) -> SchedulerJobView:
    return SchedulerJobView(
        name=job.name,
        interval_secs=job.interval_secs,
        ttl_secs=job.ttl_secs,
        next_run_at=job.next_run_at,
        last_run_at=job.last_run_at,
        last_status=job.last_status,
        has_handler=is_registered(job.name),
    )


def list_jobs(session: Session) -> list[SchedulerJobView]:
    rows = session.exec(select(ScheduledJob).order_by(ScheduledJob.name)).all()
    return [_to_view(r) for r in rows]


def get_job(session: Session, name: str) -> SchedulerJobView | None:
    job = session.get(ScheduledJob, name)
    return _to_view(job) if job else None


async def run_now(session: Session, name: str) -> RunNowResult:
    """Submit the CronWorkflow replacing ``name`` as a one-off Workflow."""
    job = session.get(ScheduledJob, name)
    if job is None:
        return RunNowResult(
            job=name,
            workflow_name=None,
            namespace="",
            status_code=404,
            message=f"unknown job: {name}",
        )

    namespace = os.environ.get("SCHEDULER_WORKFLOW_NAMESPACE", "")
    if not namespace:
        return RunNowResult(
            job=name,
            workflow_name=None,
            namespace="",
            status_code=503,
            message="SCHEDULER_WORKFLOW_NAMESPACE is not configured",
        )
    kubernetes = KubernetesClient()
    try:
        cronworkflows = await kubernetes.list_cronworkflows(namespace)
        cronworkflow = next(
            (
                item
                for item in cronworkflows
                if (item.get("metadata", {}).get("annotations") or {}).get(
                    _REPLACES_ANNOTATION
                )
                == name
            ),
            None,
        )
        if cronworkflow is None:
            return RunNowResult(
                job=name,
                workflow_name=None,
                namespace=namespace,
                status_code=409,
                message=f"no CronWorkflow replaces job {name}",
            )

        cron_metadata = cronworkflow.get("metadata") or {}
        cron_spec = cronworkflow.get("spec") or {}
        cron_name = cron_metadata["name"]
        workflow_namespace = cron_metadata.get("namespace") or namespace
        workflow_metadata = deepcopy(cron_spec.get("workflowMetadata") or {})
        workflow_metadata.update(
            {
                "generateName": f"{cron_name}-manual-",
                "namespace": workflow_namespace,
            }
        )
        workflow_metadata.pop("name", None)
        workflow_metadata["labels"] = {
            **(cron_metadata.get("labels") or {}),
            **(workflow_metadata.get("labels") or {}),
            _CRON_WORKFLOW_LABEL: cron_name,
        }
        manifest = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": workflow_metadata,
            "spec": deepcopy(cron_spec["workflowSpec"]),
        }
        workflow_name = await kubernetes.create_workflow(workflow_namespace, manifest)
        return RunNowResult(
            job=name,
            workflow_name=workflow_name,
            namespace=workflow_namespace,
            status_code=202,
        )
    except ApiException as exc:
        return RunNowResult(
            job=name,
            workflow_name=None,
            namespace=namespace,
            status_code=502,
            message=f"Kubernetes API error: {exc}",
        )
    finally:
        await kubernetes.close()
