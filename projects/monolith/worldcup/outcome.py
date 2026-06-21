"""Elo -> scoreline sampler (Dixon-Coles bivariate model, neutral venue).

Two refinements over a plain independent-Poisson split:

  * Per-team attack / defence factors. A side's scoring rate scales with its own
    attack and with the opponent's defensive leakiness, and vice versa, so a
    strong attack paired with a leaky defence is representable, which a single
    strength scalar cannot do. The factors are multiplicative around 1.0, so a
    team with no games played (factor 1.0) reduces to the plain Elo split.
  * A Dixon-Coles low-score dependence parameter ``rho``. Independent Poisson is
    known to mis-fit the 0-0 / 1-0 / 0-1 / 1-1 cells, exactly where draws and
    one-goal margins (the points and goal-difference ties the qualification
    question hinges on) live; ``rho`` reweights those four cells.

With ``att = def = 1.0`` and ``rho = 0.0`` the model is byte-identical to the
original Elo-split independent Poisson, so callers that pass neither argument
are unaffected.
"""

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


def _dc_tau(h: int, a: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Dixon-Coles low-score correction weight for the (h, a) cell.

    Returns 1.0 outside the four low-score cells. Clamped at 0 so a large lambda
    paired with a strongly negative rho can never produce a negative acceptance
    weight (which would corrupt the rejection sampler below).
    """
    if h == 0 and a == 0:
        tau = 1.0 - lam_h * lam_a * rho
    elif h == 0 and a == 1:
        tau = 1.0 + lam_h * rho
    elif h == 1 and a == 0:
        tau = 1.0 + lam_a * rho
    elif h == 1 and a == 1:
        tau = 1.0 - rho
    else:
        return 1.0
    return tau if tau > 0.0 else 0.0


def sample_scoreline(
    elo_home: float,
    elo_away: float,
    rng: random.Random,
    att_home: float = 1.0,
    def_home: float = 1.0,
    att_away: float = 1.0,
    def_away: float = 1.0,
    rho: float = 0.0,
) -> tuple[int, int]:
    """Sample a scoreline from the Dixon-Coles model.

    ``att_*`` / ``def_*`` are multiplicative factors around 1.0: the home team's
    scoring rate scales with its own attack and the away team's defensive
    leakiness. ``rho`` applies the low-score dependence correction. With the
    defaults this is the plain Elo-split independent Poisson.
    """
    we = win_expectancy(elo_home, elo_away)
    lam_h = AVG_TOTAL * we * att_home * def_away
    lam_a = AVG_TOTAL * (1.0 - we) * att_away * def_home
    if rho == 0.0:
        return _poisson(lam_h, rng), _poisson(lam_a, rng)
    # Rejection sampling: the proposal is the independent Poisson and the target
    # is proportional to proposal * tau, so accept (h, a) with probability
    # tau / m where m bounds tau over every cell. Only the four low cells can
    # exceed 1, so m is the max of those and 1.
    m = max(
        1.0,
        _dc_tau(0, 0, lam_h, lam_a, rho),
        _dc_tau(1, 1, lam_h, lam_a, rho),
    )
    while True:
        h = _poisson(lam_h, rng)
        a = _poisson(lam_a, rng)
        if rng.random() * m <= _dc_tau(h, a, lam_h, lam_a, rho):
            return h, a


def outcome_probabilities(
    elo_home: float,
    elo_away: float,
    rng: random.Random,
    n: int = 20000,
    rho: float = 0.0,
    att_home: float = 1.0,
    def_home: float = 1.0,
    att_away: float = 1.0,
    def_away: float = 1.0,
) -> dict[str, float]:
    hw = d = aw = 0
    for _ in range(n):
        h, a = sample_scoreline(
            elo_home,
            elo_away,
            rng,
            att_home=att_home,
            def_home=def_home,
            att_away=att_away,
            def_away=def_away,
            rho=rho,
        )
        if h > a:
            hw += 1
        elif h == a:
            d += 1
        else:
            aw += 1
    return {"home_win": hw / n, "draw": d / n, "away_win": aw / n}
