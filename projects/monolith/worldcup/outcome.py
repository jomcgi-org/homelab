"""Elo -> scoreline sampler. Independent-Poisson model, neutral venue."""

from __future__ import annotations

import math
import random

AVG_TOTAL = 2.6  # average total goals per match (WC-era baseline)


def win_expectancy(elo_home: float, elo_away: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(elo_home - elo_away) / 400.0))


def _poisson(lam: float, rng: random.Random) -> int:
    # Knuth's algorithm; lam is small (<3) so this is cheap.
    target = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= target:
            return k - 1


def sample_scoreline(
    elo_home: float, elo_away: float, rng: random.Random
) -> tuple[int, int]:
    we = win_expectancy(elo_home, elo_away)
    return _poisson(AVG_TOTAL * we, rng), _poisson(AVG_TOTAL * (1 - we), rng)


def outcome_probabilities(
    elo_home: float, elo_away: float, rng: random.Random, n: int = 20000
) -> dict[str, float]:
    hw = d = aw = 0
    for _ in range(n):
        h, a = sample_scoreline(elo_home, elo_away, rng)
        if h > a:
            hw += 1
        elif h == a:
            d += 1
        else:
            aw += 1
    return {"home_win": hw / n, "draw": d / n, "away_win": aw / n}
