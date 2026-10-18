"""Tests for chat.safeguards_train_job: the forest trainer one-shot."""

import asyncio
import json
import random
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from chat import safeguards, safeguards_train_job
from chat.models import ModerationEvent, TrustModel

N_FEATURES = len(safeguards.FEATURE_NAMES)


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    """File-backed SQLite engine with the chat schema stripped for SQLite compat."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'safeguards.db'}",
        connect_args={"check_same_thread": False},
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()
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


def _model_snapshot(engine) -> dict:
    with Session(engine) as session:
        return {
            row.version: row.model_dump()
            for row in session.exec(select(TrustModel)).all()
        }


@pytest.fixture
def existing_models(engine):
    with Session(engine) as session:
        session.add_all(
            [
                TrustModel(version=1, status="shadow"),
                TrustModel(version=2, status="live"),
            ]
        )
        session.commit()
    return _model_snapshot(engine)


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
        with patch("core.db.get_engine", return_value=engine):
            dataset = safeguards_train_job._gather_dataset(now)
        assert len(dataset["X"]) == 2
        assert sorted(dataset["y"]) == [0, 1]


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
            "trees": [
                {
                    "feature": [-1],
                    "threshold": [0.0],
                    "left": [-1],
                    "right": [-1],
                    "value": [0.5],
                }
            ],
            "metrics": {"oob_accuracy": 0.9},
        }
        with Session(engine) as session:
            session.add(TrustModel(version=1, status="shadow"))
            session.add(TrustModel(version=2, status="live"))
            session.commit()
        with patch("core.db.get_engine", return_value=engine):
            version = safeguards_train_job._store_model(forest, 300, 40, "local")
        assert version == 3
        with Session(engine) as session:
            rows = {r.version: r for r in session.exec(select(TrustModel)).all()}
            assert rows[1].status == "retired"
            assert rows[2].status == "live"  # promotion state is never touched
            assert rows[3].status == "shadow"
            metrics = json.loads(rows[3].metrics_json)
            assert metrics["trained_in"] == "local"
            assert json.loads(rows[3].feature_names_json) == list(
                safeguards.FEATURE_NAMES
            )


class TestHandler:
    @pytest.mark.parametrize(
        "forest",
        [
            {"n_features": N_FEATURES, "trees": [{}]},
            {
                "n_features": N_FEATURES - 1,
                "trees": [
                    {
                        "feature": [-1],
                        "threshold": [0.0],
                        "left": [-1],
                        "right": [-1],
                        "value": [0.5],
                    }
                ],
            },
        ],
        ids=["malformed-tree", "wrong-feature-count"],
    )
    def test_skips_unusable_trained_forest(
        self, engine, existing_models, forest, caplog
    ):
        dataset = _dataset()
        with (
            patch.object(safeguards_train_job, "_gather_dataset", return_value=dataset),
            patch.object(
                safeguards_train_job.safeguards_forest,
                "train_forest",
                return_value=forest,
            ) as train,
            patch.object(
                safeguards_train_job,
                "_store_model",
                wraps=safeguards_train_job._store_model,
            ) as store,
            patch.object(safeguards, "invalidate_model_cache") as invalidate,
            patch("core.db.get_engine", return_value=engine),
            caplog.at_level("INFO", logger=safeguards_train_job.logger.name),
        ):
            asyncio.run(safeguards_train_job.safeguards_train_handler(MagicMock()))
        train.assert_called_once_with(
            dataset["X"],
            dataset["y"],
            n_trees=safeguards_train_job._N_TREES,
            seed=safeguards_train_job._SEED,
        )
        store.assert_not_called()
        invalidate.assert_not_called()
        assert _model_snapshot(engine) == existing_models
        assert "safeguards-train: trained forest unusable; skipping" in caplog.text

    @pytest.mark.parametrize(
        "labels",
        [[1] * 5 + [0] * 5, [1] * 300, [0] * 300],
        ids=["too-few-samples", "no-negatives", "no-positives"],
    )
    def test_skips_on_thin_dataset(self, engine, existing_models, labels):
        thin = {"X": [[0.0] * N_FEATURES] * len(labels), "y": labels}
        with (
            patch.object(safeguards_train_job, "_gather_dataset", return_value=thin),
            patch.object(
                safeguards_train_job.safeguards_forest, "train_forest"
            ) as train,
            patch.object(safeguards_train_job, "_store_model") as store,
            patch.object(safeguards, "invalidate_model_cache") as invalidate,
            patch("core.db.get_engine", return_value=engine),
        ):
            asyncio.run(safeguards_train_job.safeguards_train_handler(MagicMock()))
        train.assert_not_called()
        store.assert_not_called()
        invalidate.assert_not_called()
        assert _model_snapshot(engine) == existing_models

    def test_trains_in_process(self, engine, existing_models):
        dataset = _dataset()
        with (
            patch.object(safeguards_train_job, "_gather_dataset", return_value=dataset),
            patch.object(safeguards, "invalidate_model_cache") as invalidate,
            patch("core.db.get_engine", return_value=engine),
        ):
            asyncio.run(safeguards_train_job.safeguards_train_handler(MagicMock()))
        invalidate.assert_called_once_with()
        stored = _model_snapshot(engine)
        assert set(stored) == {1, 2, 3}
        assert stored[1] == {**existing_models[1], "status": "retired"}
        assert stored[2] == existing_models[2]
        with Session(engine) as session:
            row = session.exec(select(TrustModel).where(TrustModel.version == 3)).one()
            assert row.status == "shadow"
            assert row.n_samples == 300
            assert row.n_positive == sum(dataset["y"])
            assert safeguards_train_job._forest_usable(json.loads(row.model_json))
            metrics = json.loads(row.metrics_json)
            assert metrics["trained_in"] == "local"
            assert metrics["oob_accuracy"] > 0.8
