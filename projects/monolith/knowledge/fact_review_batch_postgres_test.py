"""Real PostgreSQL serialization for batch admission and receipt application."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

from sqlmodel import Session, create_engine, select

from knowledge import fact_review as review
from knowledge import fact_review_batch as batch
from knowledge.freshness import STANDARD
from knowledge.models import FactReviewBatch, FactReviewRun, Note, ReviewOutcome


def test_concurrent_batches_reserve_distinct_claims_and_replay_once(pg, monkeypatch):
    identity = uuid4().hex
    scope = f"repo:batch-test/{identity}"
    now = datetime.now(UTC)
    engine = create_engine(pg.url)
    monkeypatch.setenv("KG_FACT_REVIEW_ENABLED", "true")
    monkeypatch.setenv("KG_FACT_REVIEW_BATCH_SIZE", "2")
    monkeypatch.setenv("KG_FACT_REVIEW_MAX_DAILY", "20000")
    monkeypatch.setattr(review, "_get_repo_scope", lambda: scope)
    barrier = Barrier(2)

    class Sources:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def pin(self):
            barrier.wait(timeout=10)
            return "a" * 40

        def collect(self, claim, revision):
            return [
                {
                    "id": "s1",
                    "subject": "file:projects/a.py",
                    "url": f"https://github.com/jomcgi-org/homelab/blob/{revision}/projects/a.py",
                    "text": "if not inputs: raise ValueError('empty inputs')",
                }
            ]

        def metrics(self):
            return {"source_requests": 2}

    monkeypatch.setattr(batch, "BatchSources", Sources)
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    Note(
                        note_id=f"batch-{identity}-{i}",
                        path=f"batch-{identity}-{i}.md",
                        title="Empty inputs fail",
                        content="projects/a.py rejects empty inputs before dispatch",
                        content_hash=f"hash-{i}",
                        type="fact",
                        scope=scope,
                        verification_state="verified",
                        observed_at=now - timedelta(days=100),
                        review_after=now - timedelta(days=1),
                        review_policy=STANDARD,
                    )
                    for i in range(4)
                ]
            )
            session.commit()
        keys = [f"batch-{identity}-{i}" for i in range(2)]

        def prepare(key):
            with Session(engine) as session:
                return batch.prepare(session, key, now=now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            prompts = list(pool.map(prepare, keys))
        assert all("BEGIN UNTRUSTED REVIEW DATA" in prompt for prompt in prompts)
        with Session(engine) as session:
            parents = session.exec(
                select(FactReviewBatch).where(FactReviewBatch.invocation_key.in_(keys))
            ).all()
            members = [key for parent in parents for key in parent.member_keys]
            runs = session.exec(
                select(FactReviewRun).where(FactReviewRun.invocation_key.in_(members))
            ).all()
            assert len(runs) == len({run.note_id for run in runs}) == 4
            parent = session.get(FactReviewBatch, keys[0])
            claims = [
                session.get(FactReviewRun, member) for member in parent.member_keys
            ]
            output = json.dumps(
                {
                    "reviews": [
                        {
                            "id": f"f{i}",
                            "claim_hash": review._digest(run.claim),
                            "verdict": "holds",
                            "rationale": "The complete source rejects empty inputs before dispatch.",
                            "citations": [
                                {
                                    "source_id": run.sources[0]["id"],
                                    "quote": "raise ValueError('empty inputs')",
                                }
                            ],
                        }
                        for i, run in enumerate(claims, 1)
                    ]
                }
            )
            note_ids = [run.note_id for run in claims]
        start = Barrier(2)

        def apply(_):
            with Session(engine) as session:
                # Prime stale ORM objects before contending on the receipt lock.
                cached = [session.get(FactReviewRun, member) for member in members]
                assert all(run.status == "prepared" for run in cached)
                start.wait(timeout=10)
                return batch.apply(
                    session, keys[0], output, now=now + timedelta(seconds=5)
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, range(2)))
        assert all(result["outcomes"] == {"renewed": 2} for result in results)
        with Session(engine) as session:
            outcomes = session.exec(
                select(ReviewOutcome).where(ReviewOutcome.note_id.in_(note_ids))
            ).all()
            assert len(outcomes) == 2 and all(
                row.status == "success" for row in outcomes
            )
    finally:
        engine.dispose()
