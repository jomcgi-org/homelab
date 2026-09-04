"""Repo-docs ingest: reconcile the baked markdown manifest into isolated
knowledge.repo_docs / repo_doc_chunks tables for public-chat grounding.

Confinement, isolation, and the async/sync split are documented inline at the
relevant functions. This module is imported only by the private binary's
scheduler wiring; the public binary never runs the reconcile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from knowledge.chunker import Chunk, chunk_markdown

logger = logging.getLogger(__name__)

# The manifest sits beside this module in the image runfiles (the :main binary's
# data). An env override exists purely for tests / ops.
_MANIFEST_NAME = "repo_docs_manifest.ndjson"


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    title: str
    content: str


def manifest_path() -> Path:
    override = os.environ.get("REPO_DOCS_MANIFEST_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / _MANIFEST_NAME


def load_manifest(path: Path | None = None) -> list[ManifestEntry]:
    p = path or manifest_path()
    if not p.exists():
        logger.warning("repo_docs: manifest not found at %s; nothing to index", p)
        return []
    entries: list[ManifestEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        entries.append(
            ManifestEntry(
                path=o["path"],
                sha256=o["sha256"],
                title=o["title"],
                content=o["content"],
            )
        )
    return entries


@dataclass
class ReconcilePlan:
    to_upsert: list[tuple[ManifestEntry, list[Chunk]]]
    to_delete: list[str]


def _title_for(entry: ManifestEntry) -> str:
    # The generator already derived the title; trust it (fallback already applied).
    return entry.title
