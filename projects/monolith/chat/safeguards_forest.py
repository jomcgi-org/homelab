"""Dependency-light random forest for the Bosun safeguards (ADR chat/003).

Training uses numpy only; inference (``predict_forest``) is pure Python over
a JSON-serializable tree ensemble. The split matters: training runs inside the
Firecracker sandbox guest (which has numpy transitively via pandas/scipy) or,
as a fallback, in the ephemeral trainer job pod, while inference runs on the
bot's hot path where walking ~50 shallow trees over 16 features costs
microseconds and needs no ML dependency at all.

This module MUST stay standalone: no monolith imports, numpy only, because
chat.safeguards_train_job ships its literal source into the sandbox with
``inspect.getsource`` and appends a driver. Anything imported here must exist
in the sandbox guest image too.

The forest is classic CART + bagging: bootstrap sample per tree, gini
impurity, sqrt-feature subsampling per split, out-of-bag probability estimates
for the reported metrics. Deterministic for a given (dataset, seed).

Serialized form (one dict, JSON-safe): ``n_features``, ``trees`` (parallel
arrays per tree: ``feature`` is -1 on leaves, ``threshold``/``left``/``right``
are split params and child indices, ``value`` is the leaf's positive-class
probability), and ``metrics`` (oob accuracy / auc / positive rate).
"""

from __future__ import annotations

import math


def train_forest(
    X: list[list[float]],
    y: list[int],
    *,
    n_trees: int = 48,
    max_depth: int = 8,
    min_leaf: int = 3,
    seed: int = 7,
) -> dict:
    """Fit a random forest classifier and return its JSON-safe serialization.

    ``X`` is n_samples x n_features, ``y`` is 0/1 labels. Raises ValueError on
    an empty or single-class dataset (a model that can only ever say one thing
    is worse than no model: the loader would trust it blindly).
    """
    import numpy as np

    Xa = np.asarray(X, dtype=np.float64)
    ya = np.asarray(y, dtype=np.int64)
    if Xa.ndim != 2 or Xa.shape[0] == 0:
        raise ValueError("empty dataset")
    if ya.shape[0] != Xa.shape[0]:
        raise ValueError("X/y length mismatch")
    classes = set(ya.tolist())
    if classes - {0, 1}:
        raise ValueError("labels must be 0/1")
    if len(classes) < 2:
        raise ValueError("single-class dataset; refusing to train")

    n, f = Xa.shape
    k = max(1, int(math.sqrt(f)))
    rng = np.random.default_rng(seed)

    trees: list[dict] = []
    oob_sum = np.zeros(n, dtype=np.float64)
    oob_cnt = np.zeros(n, dtype=np.int64)

    for _ in range(n_trees):
        boot = rng.integers(0, n, size=n)
        in_bag = np.zeros(n, dtype=bool)
        in_bag[boot] = True
        tree = _build_tree(Xa, ya, boot, rng, k, max_depth, min_leaf)
        trees.append(tree)
        oob_idx = np.flatnonzero(~in_bag)
        if oob_idx.size:
            for i in oob_idx.tolist():
                oob_sum[i] += _predict_tree(tree, Xa[i])
                oob_cnt[i] += 1

    metrics = _oob_metrics(ya, oob_sum, oob_cnt)
    return {
        "n_features": int(f),
        "n_trees": int(n_trees),
        "max_depth": int(max_depth),
        "seed": int(seed),
        "trees": trees,
        "metrics": metrics,
    }


def predict_forest(forest: dict, features: list[float]) -> float:
    """Positive-class probability for one feature vector: the mean of the leaf
    probabilities across trees. Pure Python; safe on the bot's hot path.
    Raises ValueError on a feature-count mismatch (schema drift)."""
    if len(features) != forest["n_features"]:
        raise ValueError(
            f"feature count mismatch: model expects {forest['n_features']}, "
            f"got {len(features)}"
        )
    trees = forest["trees"]
    if not trees:
        raise ValueError("empty forest")
    total = 0.0
    for tree in trees:
        total += _predict_tree(tree, features)
    return total / len(trees)


# --- internals ----------------------------------------------------------------


def _build_tree(Xa, ya, boot, rng, k, max_depth, min_leaf) -> dict:
    """Grow one CART tree on a bootstrap sample; return parallel-array form."""
    import numpy as np

    feature: list[int] = []
    threshold: list[float] = []
    left: list[int] = []
    right: list[int] = []
    value: list[float] = []

    def _leaf(idx) -> int:
        node = len(feature)
        feature.append(-1)
        threshold.append(0.0)
        left.append(-1)
        right.append(-1)
        value.append(float(ya[idx].mean()))
        return node

    def _grow(idx, depth) -> int:
        y_sub = ya[idx]
        if depth >= max_depth or idx.size < 2 * min_leaf or y_sub.min() == y_sub.max():
            return _leaf(idx)
        best = _best_split(Xa, ya, idx, rng, k, min_leaf)
        if best is None:
            return _leaf(idx)
        feat, thr = best
        mask = Xa[idx, feat] <= thr
        left_idx = idx[mask]
        right_idx = idx[~mask]
        node = len(feature)
        feature.append(int(feat))
        threshold.append(float(thr))
        left.append(-1)
        right.append(-1)
        value.append(float(y_sub.mean()))
        # Children are appended after the parent, so indices are backpatched.
        left[node] = _grow(left_idx, depth + 1)
        right[node] = _grow(right_idx, depth + 1)
        return node

    _grow(np.asarray(boot), 0)
    return {
        "feature": feature,
        "threshold": threshold,
        "left": left,
        "right": right,
        "value": value,
    }


def _best_split(Xa, ya, idx, rng, k, min_leaf):
    """The (feature, threshold) with the largest gini gain over a random
    k-feature subset, or None when no split beats the parent impurity while
    keeping both children at min_leaf. Vectorized over sorted prefix sums."""
    import numpy as np

    n = idx.size
    y_sub = ya[idx].astype(np.float64)
    parent_pos = y_sub.sum()
    parent_gini = 1.0 - ((parent_pos / n) ** 2 + ((n - parent_pos) / n) ** 2)
    if parent_gini <= 0.0:
        return None

    feats = rng.choice(Xa.shape[1], size=min(k, Xa.shape[1]), replace=False)
    best_gain = 1e-12
    best = None
    for feat in feats.tolist():
        vals = Xa[idx, feat]
        order = np.argsort(vals, kind="stable")
        v_sorted = vals[order]
        y_sorted = y_sub[order]
        pos_prefix = np.cumsum(y_sorted)
        # Candidate split after position i (1-based left size), only where the
        # value actually changes so both sides are non-degenerate.
        sizes_l = np.arange(1, n)
        distinct = v_sorted[1:] != v_sorted[:-1]
        valid = distinct & (sizes_l >= min_leaf) & ((n - sizes_l) >= min_leaf)
        if not valid.any():
            continue
        pos_l = pos_prefix[:-1]
        sizes_r = n - sizes_l
        pos_r = parent_pos - pos_l
        gini_l = 1.0 - ((pos_l / sizes_l) ** 2 + ((sizes_l - pos_l) / sizes_l) ** 2)
        gini_r = 1.0 - ((pos_r / sizes_r) ** 2 + ((sizes_r - pos_r) / sizes_r) ** 2)
        gain = parent_gini - (sizes_l * gini_l + sizes_r * gini_r) / n
        gain = np.where(valid, gain, -1.0)
        i = int(np.argmax(gain))
        if gain[i] > best_gain:
            best_gain = float(gain[i])
            best = (feat, float((v_sorted[i] + v_sorted[i + 1]) / 2.0))
    return best


def _predict_tree(tree: dict, features) -> float:
    """Walk one tree to its leaf probability. Accepts any indexable row."""
    feature = tree["feature"]
    node = 0
    while feature[node] != -1:
        if features[feature[node]] <= tree["threshold"][node]:
            node = tree["left"][node]
        else:
            node = tree["right"][node]
    return tree["value"][node]


def _oob_metrics(ya, oob_sum, oob_cnt) -> dict:
    """Out-of-bag accuracy, AUC, and base rate; NaN-free JSON-safe floats.
    AUC is the rank statistic (probability a random positive outranks a random
    negative, ties counted half), computed directly from the oob scores."""

    covered = oob_cnt > 0
    n_cov = int(covered.sum())
    out = {
        "oob_coverage": float(n_cov / len(ya)) if len(ya) else 0.0,
        "positive_rate": float(ya.mean()) if len(ya) else 0.0,
        "oob_accuracy": None,
        "oob_auc": None,
    }
    if n_cov == 0:
        return out
    scores = oob_sum[covered] / oob_cnt[covered]
    truth = ya[covered]
    out["oob_accuracy"] = float(((scores >= 0.5).astype(int) == truth).mean())
    pos = scores[truth == 1]
    neg = scores[truth == 0]
    if pos.size and neg.size:
        # Rank-sum AUC via broadcasting; oob sets are small (<= dataset cap).
        wins = (pos[:, None] > neg[None, :]).sum()
        ties = (pos[:, None] == neg[None, :]).sum()
        out["oob_auc"] = float((wins + 0.5 * ties) / (pos.size * neg.size))
    return out
