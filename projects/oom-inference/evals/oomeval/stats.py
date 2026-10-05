"""Item-clustered means, paired bootstrap and McNemar's exact test."""

import math
import random
import statistics


def item_means(records):
    """Mean score per item id (averaging over samples)."""
    by_item = {}
    for r in records:
        by_item.setdefault(r["id"], []).append(r["score"])
    return {k: sum(v) / len(v) for k, v in by_item.items()}


def mean_ci(values):
    """Mean and 95% half-width (normal approximation over items)."""
    n = len(values)
    if n == 0:
        return float("nan"), float("nan")
    m = sum(values) / n
    if n < 2:
        return m, float("nan")
    return m, 1.96 * statistics.stdev(values) / math.sqrt(n)


def paired_bootstrap(a, b, iters=4000, seed=0):
    """95% CI of mean(b - a) over paired items, resampling items."""
    diffs = [y - x for x, y in zip(a, b)]
    n = len(diffs)
    if n == 0:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters) - 1]


def mcnemar_exact(only_a, only_b):
    """Two-sided exact McNemar p-value from the discordant pair counts."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def median(values):
    return statistics.median(values) if values else float("nan")
