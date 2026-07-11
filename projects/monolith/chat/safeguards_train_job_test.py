"""Tests for chat.safeguards_train_job: the forest trainer one-shot."""

import asyncio
import json
import random
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import safeguards, safeguards_train_job
from chat.models import ModerationEvent, TrustModel

N_FEATURES = len(safeguards.FEATURE_NAMES)


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _dataset(n: int = 300, seed: int = 5) -> dict:
    """A learnable synthetic dataset shaped like the real feature vectors."""
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        vec = [rng.random() for _ in range(N_FEATURES)]
        label = 1 if vec[0] > 0.5 else 0
        X.append(vec)
        y.append(label)
    return {"X": X, "y": y}


class TestGatherDataset:
    def test_gathers_labeled_rows_and_drops_stale_vectors(self, engine):
        now = datetime.now(timezone.utc)
        with Session(engine) as session:
            session.add_all(
                [
                    ModerationEvent(
                        kind="signal",
                        label=1,
                        features_json=json.dumps([1.0] * N_FEATURES),
                    ),
                    ModerationEvent(
                        kind="clean_sample",
                        label=0,
                        features_json=json.dumps([0.0] * N_FEATURES),
                    ),
                    # Unlabeled rows are not samples.
                    ModerationEvent(kind="enforcement", label=None),
                    # Stale vector from an older FEATURE_NAMES: dropped.
                    ModerationEvent(
                        kind="signal", label=1, features_json=json.dumps([1.0, 2.0])
                    ),
                    # Unparseable vector: dropped.
                    ModerationEvent(kind="signal", label=1, features_json="not json"),
                ]
            )
            session.commit()
        with patch("app.db.get_engine", return_value=engine):
            dataset = safeguards_train_job._gather_dataset(now)
        assert len(dataset["X"]) == 2
        assert sorted(dataset["y"]) == [0, 1]


class TestSandboxCode:
    def test_ships_forest_source_plus_driver(self):
        code = safeguards_train_job._sandbox_code()
        assert "def train_forest(" in code
        assert "def predict_forest(" in code
        assert "json.load" in code
        assert "forest.json" in code
        # Standalone: the sandbox has no monolith packages on its path.
        assert "from chat" not in code
        assert "import chat" not in code


class TestForestUsable:
    def test_rejects_wrong_feature_count(self):
        forest = {
            "n_features": 2,
            "trees": [
                {
                    "feature": [-1],
                    "threshold": [0.0],
                    "left": [-1],
                    "right": [-1],
                    "value": [0.5],
                }
            ],
        }
        assert safeguards_train_job._forest_usable(forest) is False

    def test_rejects_garbage(self):
        assert safeguards_train_job._forest_usable({}) is False

    def test_accepts_valid_forest(self):
        forest = {
            "n_features": N_FEATURES,
            "trees": [
                {
                    "feature": [-1],
                    "threshold": [0.0],
                    "left": [-1],
                    "right": [-1],
                    "value": [0.5],
                }
            ],
        }
        assert safeguards_train_job._forest_usable(forest) is True


class TestStoreModel:
    def test_stores_shadow_and_retires_previous_shadow(self, engine):
        forest = {
            "n_features": N_FEATURES,
            "trees": [],
            "metrics": {"oob_accuracy": 0.9},
        }
        with Session(engine) as session:
            session.add(TrustModel(version=1, status="shadow"))
            session.add(TrustModel(version=2, status="live"))
            session.commit()
        with patch("app.db.get_engine", return_value=engine):
            version = safeguards_train_job._store_model(forest, 300, 40, "sandbox")
        assert version == 3
        with Session(engine) as session:
            rows = {r.version: r for r in session.exec(select(TrustModel)).all()}
            assert rows[1].status == "retired"
            assert rows[2].status == "live"  # promotion state is never touched
            assert rows[3].status == "shadow"
            metrics = json.loads(rows[3].metrics_json)
            assert metrics["trained_in"] == "sandbox"
            assert json.loads(rows[3].feature_names_json) == list(
                safeguards.FEATURE_NAMES
            )


class TestHandler:
    def test_skips_on_thin_dataset(self, engine):
        thin = {"X": [[0.0] * N_FEATURES] * 10, "y": [1] * 5 + [0] * 5}
        with (
            patch.object(safeguards_train_job, "_gather_dataset", return_value=thin),
            patch("app.db.get_engine", return_value=engine),
        ):
            asyncio.run(safeguards_train_job.safeguards_train_handler(MagicMock()))
        with Session(engine) as session:
            assert session.exec(select(TrustModel)).all() == []

    def test_falls_back_to_local_fit_when_sandbox_fails(self, engine):
        dataset = _dataset()
        with (
            patch.object(safeguards_train_job, "_gather_dataset", return_value=dataset),
            patch.object(
                safeguards_train_job,
                "_train_in_sandbox",
                AsyncMock(return_value=None),
            ),
            patch("app.db.get_engine", return_value=engine),
        ):
            asyncio.run(safeguards_train_job.safeguards_train_handler(MagicMock()))
        with Session(engine) as session:
            row = session.exec(select(TrustModel)).one()
            assert row.status == "shadow"
            assert row.n_samples == 300
            metrics = json.loads(row.metrics_json)
            assert metrics["trained_in"] == "local"
            assert metrics["oob_accuracy"] > 0.8

    def test_uses_sandbox_forest_when_valid(self, engine):
        dataset = _dataset()
        sandbox_forest = {
            "n_features": N_FEATURES,
            "trees": [
                {
                    "feature": [-1],
                    "threshold": [0.0],
                    "left": [-1],
                    "right": [-1],
                    "value": [0.5],
                }
            ],
            "metrics": {"oob_accuracy": 0.99},
        }
        with (
            patch.object(safeguards_train_job, "_gather_dataset", return_value=dataset),
            patch.object(
                safeguards_train_job,
                "_train_in_sandbox",
                AsyncMock(return_value=sandbox_forest),
            ),
            patch("app.db.get_engine", return_value=engine),
        ):
            asyncio.run(safeguards_train_job.safeguards_train_handler(MagicMock()))
        with Session(engine) as session:
            row = session.exec(select(TrustModel)).one()
            metrics = json.loads(row.metrics_json)
            assert metrics["trained_in"] == "sandbox"

    def test_invalid_sandbox_forest_falls_back(self, engine):
        dataset = _dataset()
        with (
            patch.object(safeguards_train_job, "_gather_dataset", return_value=dataset),
            patch.object(
                safeguards_train_job,
                "_train_in_sandbox",
                AsyncMock(return_value={"n_features": 2, "trees": []}),
            ),
            patch("app.db.get_engine", return_value=engine),
        ):
            asyncio.run(safeguards_train_job.safeguards_train_handler(MagicMock()))
        with Session(engine) as session:
            row = session.exec(select(TrustModel)).one()
            metrics = json.loads(row.metrics_json)
            assert metrics["trained_in"] == "local"


class TestTrainInSandbox:
    def test_unconfigured_sandbox_returns_none(self):
        # With no FC_INVOKE_URL the client short-circuits with an error dict
        # and the trainer reports "no sandbox" as None (handler falls back).
        with patch("sandbox.client.FC_INVOKE_URL", ""):
            result = asyncio.run(safeguards_train_job._train_in_sandbox(_dataset(20)))
        assert result is None

    def test_sandbox_forest_json_decoded(self):
        import base64

        forest = {"n_features": N_FEATURES, "trees": []}
        payload = base64.b64encode(json.dumps(forest).encode()).decode()
        result_dict = {
            "exit_code": 0,
            "files": [{"path": "forest.json", "content_b64": payload}],
        }
        with patch(
            "sandbox.client.run_python_in_sandbox",
            AsyncMock(return_value=result_dict),
        ):
            result = asyncio.run(safeguards_train_job._train_in_sandbox(_dataset(20)))
        assert result == forest
