"""Tests for chat.safeguards_forest: the dependency-light random forest."""

import json
import random

import pytest

from chat.safeguards_forest import predict_forest, train_forest


def _synthetic(n: int = 300, seed: int = 3) -> tuple[list[list[float]], list[int]]:
    """A learnable dataset: feature 0 separates the classes (with a little
    label noise), features 1-3 are pure noise."""
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        signal = rng.random()
        label = 1 if signal > 0.5 else 0
        if rng.random() < 0.05:
            label = 1 - label
        X.append([signal, rng.random(), rng.random(), rng.random()])
        y.append(label)
    return X, y


class TestTrainForest:
    def test_learns_separable_data(self):
        X, y = _synthetic()
        forest = train_forest(X, y, n_trees=24, seed=7)
        assert forest["n_features"] == 4
        assert len(forest["trees"]) == 24
        assert forest["metrics"]["oob_accuracy"] > 0.85
        assert forest["metrics"]["oob_auc"] > 0.9
        assert predict_forest(forest, [0.95, 0.5, 0.5, 0.5]) > 0.5
        assert predict_forest(forest, [0.05, 0.5, 0.5, 0.5]) < 0.5

    def test_deterministic_for_seed(self):
        X, y = _synthetic()
        a = train_forest(X, y, n_trees=8, seed=11)
        b = train_forest(X, y, n_trees=8, seed=11)
        assert a["trees"] == b["trees"]

    def test_json_roundtrip_predicts_identically(self):
        X, y = _synthetic()
        forest = train_forest(X, y, n_trees=8, seed=7)
        thawed = json.loads(json.dumps(forest))
        probe = [0.7, 0.1, 0.9, 0.4]
        assert predict_forest(thawed, probe) == predict_forest(forest, probe)

    def test_rejects_empty_dataset(self):
        with pytest.raises(ValueError, match="empty"):
            train_forest([], [])

    def test_rejects_single_class(self):
        with pytest.raises(ValueError, match="single-class"):
            train_forest([[0.1], [0.2], [0.3]], [1, 1, 1])

    def test_rejects_length_mismatch(self):
        with pytest.raises(ValueError, match="mismatch"):
            train_forest([[0.1], [0.2]], [1])

    def test_rejects_non_binary_labels(self):
        with pytest.raises(ValueError, match="0/1"):
            train_forest([[0.1], [0.2]], [1, 2])


class TestPredictForest:
    def test_feature_count_mismatch_raises(self):
        X, y = _synthetic(n=100)
        forest = train_forest(X, y, n_trees=4, seed=7)
        with pytest.raises(ValueError, match="feature count mismatch"):
            predict_forest(forest, [0.5])

    def test_single_leaf_stub_tree(self):
        forest = {
            "n_features": 2,
            "trees": [
                {
                    "feature": [-1],
                    "threshold": [0.0],
                    "left": [-1],
                    "right": [-1],
                    "value": [0.9],
                }
            ],
        }
        assert predict_forest(forest, [0.0, 0.0]) == 0.9

    def test_empty_forest_raises(self):
        with pytest.raises(ValueError, match="empty forest"):
            predict_forest({"n_features": 1, "trees": []}, [0.0])
