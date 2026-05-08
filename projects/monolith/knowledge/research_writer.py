"""Vault writers for research raws and failed-research quarantine files.

Both writers produce idempotent, byte-stable markdown with YAML
frontmatter suitable for raw_ingest pickup. The schema of the
frontmatter is the ground-truth provenance for every atom that gets
committed downstream.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from knowledge.research_agent import (
    PIPELINE_VERSION,
    Claim,
    SourceEntry,
)

INBOX_RESEARCH_DIR = "_inbox/research"
FAILED_RESEARCH_DIR = "_failed_research"


def _source_to_fm(s: SourceEntry) -> dict[str, Any]:
    """Serialize a SourceEntry to a frontmatter-friendly dict.

    Drops empty / None values so frontmatter stays tight; today both
    fields are always populated, but kept defensive for future shape
    extensions.
    """
    return {k: v for k, v in asdict(s).items() if v not in (None, [], "")}


def _yaml_dump(fm: dict) -> str:
    return yaml.dump(fm, default_flow_style=False, sort_keys=False)


def write_research_raw(
    *,
    vault_root: Path,
    slug: str,
    title: str,
    summary: str,
    supported_claims: list[Claim],
    sources: list[SourceEntry],
    agent_model: str,
    researched_at: str,
) -> Path:
    """Write the post-filter research raw to ``_inbox/research/<slug>.md``.

    ``supported_claims`` is the post-filter claim list -- every claim
    has at least one source_ref that the agent actually retrieved.
    """
    out_dir = vault_root / INBOX_RESEARCH_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.md"

    fm = {
        "type": "research",
        "visibility": "public",
        "id": slug,
        "title": f"Research note: {title}",
        "derived_from_gap": slug,
        "agent_model": agent_model,
        "pipeline_version": PIPELINE_VERSION,
        "researched_at": researched_at,
        "sources": [_source_to_fm(s) for s in sources],
        "claims_supported": len(supported_claims),
    }

    body_lines = [f"## Summary\n\n{summary}\n", "## Supported claims"]
    for c in supported_claims:
        refs = ", ".join(c.source_refs)
        body_lines.append(f"- {c.text} _[{refs}]_")
    body_lines.append("")
    body_lines.append("## Sources")
    for s in sources:
        body_lines.append(f"- {s.tool}: {s.ref}")

    body = "\n".join(body_lines) + "\n"
    out_path.write_text(f"---\n{_yaml_dump(fm)}---\n\n{body}")
    return out_path


def quarantine(
    *,
    vault_root: Path,
    slug: str,
    attempt: int,
    summary: str,
    pre_filter_claims: list[Claim],
    sources: list[SourceEntry],
    agent_model: str,
    researched_at: str,
) -> Path:
    """Write a fully-rejected research draft to ``_failed_research/<slug>-<N>.md``.

    A draft is "rejected" when the post-filter claim list is empty -- i.e.
    every claim cited a source the agent didn't actually retrieve. The
    pre-filter claims are persisted for forensics so we can see what
    Sonnet *tried* to say even though the citations didn't check out.
    """
    out_dir = vault_root / FAILED_RESEARCH_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}-{attempt}.md"

    fm = {
        "type": "failed_research",
        "id": f"{slug}-{attempt}",
        "derived_from_gap": slug,
        "attempt": attempt,
        "agent_model": agent_model,
        "pipeline_version": PIPELINE_VERSION,
        "researched_at": researched_at,
        "sources_attempted": [_source_to_fm(s) for s in sources],
        "claims_emitted": len(pre_filter_claims),
    }

    body_lines = [
        f"# Failed research draft (attempt {attempt})",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Claims (pre-filter)",
    ]
    for c in pre_filter_claims:
        refs = ", ".join(c.source_refs) if c.source_refs else "(no refs)"
        body_lines.append(f"- {c.text} _[{refs}]_")
    body = "\n".join(body_lines) + "\n"
    out_path.write_text(f"---\n{_yaml_dump(fm)}---\n\n{body}")
    return out_path
