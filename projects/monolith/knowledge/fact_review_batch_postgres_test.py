"""Real PostgreSQL serialization for batch admission and receipt application."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

from sqlmodel import Session, create_engine, select

from knowledge import fact_review as review
from knowledge import fact_review_batch as batch
from knowledge.freshness import STANDARD
from knowledge.models import (
    FactReviewBatch,
    FactReviewRun,
    FactReviewSourceBlock,
    Note,
    ReviewOutcome,
)


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
            self.stopped = None
            self.requests = 0
            self.cache_hits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def pin(self):
            self.requests += 1
            barrier.wait(timeout=10)
            return "a" * 40

        def collect(self, claim, revision):
            self.requests += 1
            return [
                {
                    "id": "s1",
                    "subject": "file:projects/a.py",
                    "url": f"https://github.com/jomcgi-org/homelab/blob/{revision}/projects/a.py",
                    "text": "if not inputs: raise ValueError('empty inputs')",
                }
            ]

        def metrics(self):
            return {"source_requests": self.requests}

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
                        review_after=now - timedelta(days=10),
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


def test_coverage_migration_restart_and_concurrent_probe_reservations(pg, monkeypatch):
    """Uses the migration-created table and real PostgreSQL admission locks."""
    from knowledge.review_source_blocks import probe
    from knowledge.review_sources import collect_sources

    identity = uuid4().hex
    scope = f"repo:coverage-test/{identity}"
    repo = scope.removeprefix("repo:")
    now = datetime.now(UTC)
    engine = create_engine(pg.url)
    monkeypatch.setenv("KG_FACT_REVIEW_ENABLED", "true")
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    monkeypatch.setenv("KG_FACT_REVIEW_MAX_DAILY", "20000")
    monkeypatch.setattr(review, "_get_repo_scope", lambda: scope)
    raw = b"bounded_identity = True\n" * 3000
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    def fetch(url):
        if "/git/ref/" in url:
            return {"object": {"sha": "a" * 40}}
        if "/git/trees/" in url:
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "source.py",
                        "size": len(raw),
                        "sha": sha,
                        "type": "blob",
                        "mode": "100644",
                    }
                ],
            }
        return {
            "sha": sha,
            "size": len(raw),
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }

    ids = [f"coverage-{identity}-{i}" for i in range(4)]
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    Note(
                        note_id=key,
                        path=key + ".md",
                        title="General source behavior",
                        content="projects/source.py proves runtime behavior",
                        content_hash=key,
                        type="fact",
                        scope=scope,
                        verification_state="verified",
                        observed_at=now - timedelta(days=91),
                        review_after=now - timedelta(days=1),
                        review_policy=STANDARD,
                    )
                    for key in ids
                ]
            )
            session.commit()
            for index in range(4):
                review.prepare(
                    session,
                    f"coverage-run-{identity}-{index}",
                    collector=lambda claim: collect_sources(
                        claim, fetch=fetch, repo=repo
                    ),
                    probe_fetch=fetch,
                    now=now,
                )
            assert (
                len(
                    session.exec(
                        select(FactReviewSourceBlock).where(
                            FactReviewSourceBlock.note_id.in_(ids)
                        )
                    ).all()
                )
                == 4
            )
        start = Barrier(2)

        def poll(_):
            with Session(engine) as session:
                start.wait(timeout=10)
                probe(session, now + timedelta(hours=7), fetch=fetch)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(poll, range(2)))
        with Session(engine) as session:
            blocks = session.exec(
                select(FactReviewSourceBlock).where(
                    FactReviewSourceBlock.note_id.in_(ids)
                )
            ).all()
            assert sum(block.last_probe_at is not None for block in blocks) == 2
            assert not any(block.released for block in blocks)
            assert review._candidate(session, now + timedelta(hours=7)) is None
            assert (
                len(
                    session.exec(
                        select(FactReviewRun).where(FactReviewRun.note_id.in_(ids))
                    ).all()
                )
                == 4
            )
    finally:
        engine.dispose()
