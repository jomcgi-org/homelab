"""Safeguards random-forest trainer (ADR chat/003).

Runs as the ``safeguards-train`` Argo CronWorkflow one-shot (app/jobs_main.py,
chart/values.yaml jobs.cronWorkflows). Pipeline:

1. Sync DB phase (asyncio.to_thread, own session): gather the labeled
   moderation-event dataset (feature vectors + labels) over the training
   window, newest first, capped.
2. Compute phase: fit the forest OFF this pod by shipping the literal source
   of chat.safeguards_forest plus a driver into the Firecracker sandbox
   (sandbox.client.run_python_in_sandbox); the guest has numpy transitively
   via pandas/scipy and a 25s wall-clock cap, which the dataset cap and tree
   count are sized to fit. When the sandbox is unreachable or errors, fall
   back to fitting in-process (same code, this pod's CPU): the job pod is
   ephemeral anyway, so the fallback trades isolation for availability.
3. Sync DB phase: store the forest as a new chat.trust_model row with
   status='shadow' and retire superseded shadow rows. Promotion to 'live' is
   a manual decision after reviewing shadow scores on moderation events.

Skips (without writing a model) when the dataset is too small or too
one-sided to learn from: an RF fit on a handful of rows memorizes noise, and
the heuristic + LLM lanes keep enforcing regardless.

No Session ever crosses an await (monolith async/session rule; semgrep
no-sync-session-in-async-def / no-session-in-to-thread).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from chat import safeguards, safeguards_forest
from chat.models import ModerationEvent, TrustModel

logger = logging.getLogger("monolith.chat.safeguards_train")

# Training-set shape gates: below these the run skips. ~200 rows with a
# reasonable class mix is the point where oob metrics stop being pure noise.
_MIN_SAMPLES = int(os.environ.get("SAFEGUARDS_TRAIN_MIN_SAMPLES", "200"))
_MIN_POSITIVE = int(os.environ.get("SAFEGUARDS_TRAIN_MIN_POSITIVE", "25"))
_MIN_NEGATIVE = int(os.environ.get("SAFEGUARDS_TRAIN_MIN_NEGATIVE", "25"))
_WINDOW_DAYS = int(os.environ.get("SAFEGUARDS_TRAIN_WINDOW_DAYS", "90"))
# Sized with the tree count to fit the sandbox guest's 25s wall-clock cap.
_MAX_SAMPLES = int(os.environ.get("SAFEGUARDS_TRAIN_MAX_SAMPLES", "10000"))
_N_TREES = int(os.environ.get("SAFEGUARDS_TRAIN_N_TREES", "48"))
_SEED = 7


def _gather_dataset(now: datetime) -> dict:
    """Labeled feature vectors from moderation events, newest first. Rows with
    a stale feature-vector length (from an older FEATURE_NAMES) are dropped:
    the model must match the running extractor. Own session (to_thread)."""
    from core.db import get_engine

    since = now - timedelta(days=_WINDOW_DAYS)
    n_features = len(safeguards.FEATURE_NAMES)
    X: list[list[float]] = []
    y: list[int] = []
    with Session(get_engine()) as session:
        rows = session.exec(
            select(ModerationEvent.features_json, ModerationEvent.label)
            .where(ModerationEvent.label.is_not(None))
            .where(ModerationEvent.created_at >= since)
            .order_by(ModerationEvent.id.desc())
            .limit(_MAX_SAMPLES)
        ).all()
    dropped = 0
    for features_json, label in rows:
        try:
            vec = json.loads(features_json)
        except ValueError:
            dropped += 1
            continue
        if not isinstance(vec, list) or len(vec) != n_features:
            dropped += 1
            continue
        X.append([float(v) for v in vec])
        y.append(int(label))
    if dropped:
        logger.info("safeguards-train: dropped %d stale/invalid vectors", dropped)
    return {"X": X, "y": y}


def _store_model(forest: dict, n_samples: int, n_positive: int, trained_in: str) -> int:
    """Insert the new shadow model and retire superseded shadow rows. Returns
    the new version. Own session (to_thread). A 'live' row is never touched:
    promotion and demotion stay manual."""
    from core.db import get_engine

    now = datetime.now(timezone.utc)
    metrics = dict(forest.get("metrics") or {})
    metrics["trained_in"] = trained_in
    with Session(get_engine()) as session:
        latest = session.exec(
            select(TrustModel).order_by(TrustModel.version.desc())
        ).first()
        version = (latest.version if latest is not None else 0) + 1
        superseded = session.exec(
            select(TrustModel).where(TrustModel.status == "shadow")
        ).all()
        for row in superseded:
            row.status = "retired"
        if superseded:
            session.add_all(superseded)
        session.add(
            TrustModel(
                version=version,
                status="shadow",
                model_json=json.dumps(forest),
                feature_names_json=json.dumps(list(safeguards.FEATURE_NAMES)),
                n_samples=n_samples,
                n_positive=n_positive,
                metrics_json=json.dumps(metrics),
                trained_at=now,
                created_at=now,
            )
        )
        session.commit()
    return version


def _forest_usable(forest: dict) -> bool:
    """Structural sanity check on a forest before it is stored: right feature
    count and a probability that actually evaluates. The sandbox is trusted
    code but a truncated file or a partial write must not become a model."""
    try:
        if int(forest["n_features"]) != len(safeguards.FEATURE_NAMES):
            return False
        prob = safeguards_forest.predict_forest(
            forest, [0.0] * len(safeguards.FEATURE_NAMES)
        )
        return 0.0 <= prob <= 1.0
    except Exception:
        return False


async def safeguards_train_handler(session: Session) -> None:
    """Run one training pass. The ``session`` argument (one-shot CLI wrapper
    contract) is unused: all DB I/O runs in worker threads with own sessions."""
    now = datetime.now(timezone.utc)
    dataset = await asyncio.to_thread(_gather_dataset, now)
    n = len(dataset["y"])
    n_pos = sum(dataset["y"])
    n_neg = n - n_pos
    if n < _MIN_SAMPLES or n_pos < _MIN_POSITIVE or n_neg < _MIN_NEGATIVE:
        logger.info(
            "safeguards-train: dataset too thin (n=%d pos=%d neg=%d; need "
            "%d/%d/%d); skipping",
            n,
            n_pos,
            n_neg,
            _MIN_SAMPLES,
            _MIN_POSITIVE,
            _MIN_NEGATIVE,
        )
        return

    forest = await asyncio.to_thread(
        safeguards_forest.train_forest,
        dataset["X"],
        dataset["y"],
        n_trees=_N_TREES,
        seed=_SEED,
    )
    trained_in = "local"

    version = await asyncio.to_thread(_store_model, forest, n, n_pos, trained_in)
    safeguards.invalidate_model_cache()
    logger.info(
        "safeguards-train: stored shadow model v%d (n=%d pos=%d %s) metrics=%s",
        version,
        n,
        n_pos,
        trained_in,
        json.dumps(forest.get("metrics") or {}),
    )
