"""PydanticAI cluster agent -- read-only Kubernetes debugging over chat.

Mirrors ``chat.explorer``'s shape (same model/provider wiring), but the tools
call the ``cluster`` and ``agent`` domain facades instead of the knowledge
graph. Every tool opens and closes its own ``KubernetesClient``, same as the
``k8s-*`` MCP tools in ``cluster/mcp.py``, and is read-only: no ArgoCD sync
tool is registered here.
"""

import json
import logging
import os
from dataclasses import dataclass

from pydantic_ai import Agent, ModelSettings, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from chat.sse import SSEEmitter
from cluster.api import (
    KubernetesClient,
    build_health,
    dedupe_events,
    filter_logs,
    resource_detail,
    resource_row,
)
import shared.inference

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the homelab cluster assistant on Joe's private dashboard. You inspect a
Kubernetes homelab (GitOps via ArgoCD) with read-only tools and answer
operational questions plainly.

Workflow: start with health_summary for "what's broken" questions; use
list_resources/get_resource to drill in; use pod_logs and get_events for root
cause. Quote concrete evidence (restart counts, log lines, event messages) in
your answer. Be concise: a few sentences, not a report. Never invent resources
you did not observe."""

# Workload kinds scanned by the health rollup (mirrors cluster/mcp.py).
_HEALTH_KINDS = ("deployments", "statefulsets", "daemonsets", "pods", "applications")


@dataclass
class ClusterDeps:
    emitter: SSEEmitter


def create_cluster_agent() -> Agent[ClusterDeps]:
    """Create a PydanticAI agent configured for read-only cluster chat via Qwen."""
    url = os.environ.get("LLAMA_CPP_URL", "http://localhost:8000")
    model = OpenAIChatModel(
        "qwen3.6-27b",
        provider=OpenAIProvider(base_url=f"{url}/v1", api_key="not-needed"),
    )
    agent: Agent[ClusterDeps] = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        model_settings=ModelSettings(
            temperature=1.0,
            top_p=0.95,
            extra_body={
                "top_k": 20,
                "presence_penalty": 1.5,
                # The served Qwen is a hybrid thinking model: without this the
                # whole generation lands in the reasoning field, content comes
                # back null, and the SSE stream emits an empty answer.
                **shared.inference.thinking_off(),
            },
        ),
    )

    @agent.tool
    async def health_summary(ctx: RunContext[ClusterDeps]) -> str:
        """Cluster health rollup: only the unhealthy workloads, pods and ArgoCD apps."""
        ctx.deps.emitter.emit("tool_call", {"tool": "health_summary", "args": {}})
        return await _health_summary()

    @agent.tool
    async def list_resources(
        ctx: RunContext[ClusterDeps],
        kind: str,
        namespace: str | None = None,
        label_selector: str | None = None,
    ) -> str:
        """List resources of a curated kind as lean one-line rows (capped 100)."""
        ctx.deps.emitter.emit(
            "tool_call",
            {
                "tool": "list_resources",
                "args": {
                    "kind": kind,
                    "namespace": namespace,
                    "label_selector": label_selector,
                },
            },
        )
        return await _list_resources(kind, namespace, label_selector)

    @agent.tool
    async def get_resource(
        ctx: RunContext[ClusterDeps],
        kind: str,
        name: str,
        namespace: str | None = None,
    ) -> str:
        """Get one resource, trimmed to key status/conditions."""
        ctx.deps.emitter.emit(
            "tool_call",
            {
                "tool": "get_resource",
                "args": {"kind": kind, "name": name, "namespace": namespace},
            },
        )
        return await _get_resource(kind, name, namespace)

    @agent.tool
    async def pod_logs(
        ctx: RunContext[ClusterDeps],
        namespace: str,
        pod: str,
        container: str | None = None,
        tail_lines: int = 200,
        grep: str | None = None,
    ) -> str:
        """Read a pod's logs, optionally regex-filtered and tailed."""
        ctx.deps.emitter.emit(
            "tool_call",
            {
                "tool": "pod_logs",
                "args": {
                    "namespace": namespace,
                    "pod": pod,
                    "container": container,
                    "tail_lines": tail_lines,
                    "grep": grep,
                },
            },
        )
        return await _pod_logs(namespace, pod, container, tail_lines, grep)

    @agent.tool
    async def get_events(
        ctx: RunContext[ClusterDeps],
        namespace: str | None = None,
        involved_object: str | None = None,
    ) -> str:
        """List cluster events, deduplicated by (object, type, reason, message)."""
        ctx.deps.emitter.emit(
            "tool_call",
            {
                "tool": "get_events",
                "args": {"namespace": namespace, "involved_object": involved_object},
            },
        )
        return await _get_events(namespace, involved_object)

    return agent


async def _health_summary() -> str:
    k8s = KubernetesClient()
    try:
        resources: dict[str, list[dict]] = {}
        for kind in _HEALTH_KINDS:
            try:
                resources[kind] = await k8s.list_resources(kind)
            except Exception:
                logger.exception("cluster_agent: listing %s failed", kind)
                resources[kind] = []
        return json.dumps(build_health(resources))
    except Exception as exc:
        logger.exception("cluster_agent: health_summary failed")
        return f"error: {exc}"
    finally:
        await k8s.close()


async def _list_resources(
    kind: str, namespace: str | None, label_selector: str | None
) -> str:
    k8s = KubernetesClient()
    try:
        objs = await k8s.list_resources(kind, namespace, label_selector)
        rows = [resource_row(kind, o) for o in objs]
        return json.dumps({"kind": kind, "count": len(rows), "items": rows[:100]})
    except Exception as exc:
        logger.exception("cluster_agent: list_resources(%s) failed", kind)
        return f"error: {exc}"
    finally:
        await k8s.close()


async def _get_resource(kind: str, name: str, namespace: str | None) -> str:
    k8s = KubernetesClient()
    try:
        obj = await k8s.get_resource(kind, name, namespace)
        if obj is None:
            return f"error: {kind}/{name} not found"
        return json.dumps(resource_detail(kind, obj, full=False))
    except Exception as exc:
        logger.exception("cluster_agent: get_resource(%s/%s) failed", kind, name)
        return f"error: {exc}"
    finally:
        await k8s.close()


async def _pod_logs(
    namespace: str,
    pod: str,
    container: str | None,
    tail_lines: int,
    grep: str | None,
) -> str:
    k8s = KubernetesClient()
    try:
        text = await k8s.get_pod_logs(
            namespace, pod, container=container, tail_lines=tail_lines
        )
        return json.dumps(filter_logs(text, grep=grep, max_lines=tail_lines))
    except Exception as exc:
        logger.exception("cluster_agent: pod_logs(%s/%s) failed", namespace, pod)
        return f"error: {exc}"
    finally:
        await k8s.close()


async def _get_events(namespace: str | None, involved_object: str | None) -> str:
    k8s = KubernetesClient()
    try:
        events = await k8s.list_events(namespace, involved_object)
        deduped = dedupe_events(events)
        return json.dumps({"count": len(deduped), "events": deduped})
    except Exception as exc:
        logger.exception("cluster_agent: get_events failed")
        return f"error: {exc}"
    finally:
        await k8s.close()
