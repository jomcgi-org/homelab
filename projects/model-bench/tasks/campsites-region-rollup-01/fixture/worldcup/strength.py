"""Bayesian-lite team strength: an Elo posterior (rating + sigma) for the sim.

The frozen Elo snapshot (``ratings.load_elo``) is a point estimate: it carries
no uncertainty and was fixed before the tournament. This module turns it into an
approximate posterior per team by folding in the group-stage results that have
ALREADY been played:

    prior         N(snapshot_rating, sigma0**2)        # shared prior width
    x likelihood  observed finished group results      # World-Football Elo step
    -> posterior  N(updated_rating, sigma_eff**2)      # sigma shrinks with games

It is deliberately NOT full Glicko-2. With only two or three group games per
team a per-team volatility parameter has almost no data to estimate, so instead
of a rigorous joint variance we use a transparent shrink:

    sigma_eff = sigma0 / sqrt(1 + n_played / SHRINK_C)

a monotonic "more games seen -> more confident" reduction. The approximation is
called out so nobody mistakes it for a calibrated credible interval.

The posterior feeds the Monte Carlo two ways: the updated ``rating`` is the mean
strength, and ``sigma`` is the per-trial draw width that injects epistemic
uncertainty, so the sim stops pretending it knows each team's strength exactly.

Pure functions over plain dicts and dataclasses: no DB and no network, so the
unit tests drive them directly with hand-built inputs.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

# Prior standard deviation on a snapshot rating, in Elo points: how wrong the
# frozen pre-tournament snapshot could be about a team's true current strength.
PRIOR_SIGMA = float(os.environ.get("WORLDCUP_PRIOR_SIGMA", "40"))

# Elo K-factor for folding finished results forward. 40 is the World-Football
# Elo base for World Cup matches; the goal-difference multiplier scales it up.
K_FACTOR = float(os.environ.get("WORLDCUP_ELO_K", "40"))

# Variance-shrink constant. With c = 2 a team that has played two games has its
# width cut to sigma0 / sqrt(2) ~= 0.71 of the prior; an unplayed team keeps it.
SHRINK_C = float(os.environ.get("WORLDCUP_SIGMA_SHRINK_C", "2"))


@dataclass
class TeamStrength:
    rating: float  # posterior mean Elo
    sigma: float  # posterior std (Elo points), always >= 0


@dataclass
class FinishedGame:
    """A completed group fixture, replayed in matchday order for the update."""

    matchday: int
    home_code: str
    away_code: str
    home_score: int
    away_score: int


def _win_expectancy(rating_home: float, rating_away: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(rating_home - rating_away) / 400.0))


def _gd_multiplier(goal_diff: int) -> float:
    """World-Football Elo goal-difference weighting: bigger wins move more."""
    g = abs(goal_diff)
    if g <= 1:
        return 1.0
    if g == 2:
        return 1.5
    return (11.0 + g) / 8.0


def posterior_strengths(
    snapshot: dict[str, float],
    finished: list[FinishedGame],
    *,
    sigma0: float = PRIOR_SIGMA,
    k: float = K_FACTOR,
    shrink_c: float = SHRINK_C,
) -> dict[str, TeamStrength]:
    """Build the per-team Elo posterior from the snapshot plus finished results.

    Every code in ``snapshot`` gets a ``TeamStrength``. Ratings start at the
    snapshot value and are nudged by each finished game in matchday order (a
    standard zero-sum Elo update with a goal-difference multiplier), so a team
    that over- or under-performed its rating carries that into its remaining
    fixtures. ``sigma`` starts at ``sigma0`` and shrinks by the number of games
    a team has actually played. A finished game that references a code absent
    from the snapshot is skipped (the caller surfaces missing ratings loudly).
    """
    ratings = dict(snapshot)
    played: dict[str, int] = {c: 0 for c in snapshot}

    for game in sorted(finished, key=lambda g: g.matchday):
        h, a = game.home_code, game.away_code
        if h not in ratings or a not in ratings:
            continue
        we_home = _win_expectancy(ratings[h], ratings[a])
        if game.home_score > game.away_score:
            score_home = 1.0
        elif game.home_score == game.away_score:
            score_home = 0.5
        else:
            score_home = 0.0
        mult = _gd_multiplier(game.home_score - game.away_score)
        # Zero-sum: the away team's delta is exactly the negation of the home
        # team's, so the total rating mass across all teams is conserved.
        delta = k * mult * (score_home - we_home)
        ratings[h] += delta
        ratings[a] -= delta
        played[h] += 1
        played[a] += 1

    return {
        c: TeamStrength(
            rating=ratings[c],
            sigma=sigma0 / math.sqrt(1.0 + played[c] / shrink_c),
        )
        for c in snapshot
    }
