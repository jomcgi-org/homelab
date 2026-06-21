"""Analytic tests for the Bayesian-lite Elo posterior (worldcup.strength).

These exercise the pure update math directly with hand-built snapshots and
finished games, so every assertion follows from the construction with no rng and
no DB. Priors (sigma0, k, shrink_c) are passed explicitly so the tests do not
depend on the WORLDCUP_* environment overrides.
"""

import math

from worldcup.strength import FinishedGame, posterior_strengths

_SIGMA0 = 40.0
_K = 40.0
_C = 2.0


def _post(snapshot, finished):
    return posterior_strengths(snapshot, finished, sigma0=_SIGMA0, k=_K, shrink_c=_C)


def test_no_finished_games_posterior_equals_prior():
    # With no likelihood, the posterior is exactly the prior: snapshot ratings
    # unchanged and every sigma at the shared prior width.
    snapshot = {"SCO": 1700.0, "ENG": 1950.0, "AND": 1300.0}
    post = _post(snapshot, [])
    for code, rating in snapshot.items():
        assert post[code].rating == rating
        assert post[code].sigma == _SIGMA0


def test_sigma_shrinks_with_games_played():
    # sigma_eff = sigma0 / sqrt(1 + n_played / c). ENG plays twice, SCO once,
    # AND not at all, so sigma is strictly ordered AND < unplayed.
    snapshot = {"SCO": 1700.0, "ENG": 1700.0, "WAL": 1700.0, "AND": 1700.0}
    finished = [
        FinishedGame(1, "ENG", "SCO", 1, 1),  # ENG +1, SCO +1
        FinishedGame(2, "ENG", "WAL", 0, 0),  # ENG +1, WAL +1
    ]
    post = _post(snapshot, finished)
    assert post["ENG"].sigma == _SIGMA0 / math.sqrt(1 + 2 / _C)
    assert post["SCO"].sigma == _SIGMA0 / math.sqrt(1 + 1 / _C)
    assert post["AND"].sigma == _SIGMA0  # never played, prior width retained
    assert post["ENG"].sigma < post["SCO"].sigma < post["AND"].sigma


def test_winner_rating_rises_loser_falls():
    # Equal-rated teams: home wins, so home rating must rise and away must fall
    # by the same amount (and a draw left both at 1700, see below).
    snapshot = {"SCO": 1700.0, "ENG": 1700.0}
    post = _post(snapshot, [FinishedGame(1, "SCO", "ENG", 1, 0)])
    assert post["SCO"].rating > 1700.0
    assert post["ENG"].rating < 1700.0
    assert math.isclose(post["SCO"].rating - 1700.0, 1700.0 - post["ENG"].rating)


def test_draw_between_equals_leaves_ratings_unchanged():
    # we_home == 0.5 and score_home == 0.5 -> zero delta. Ratings frozen, but
    # sigma still shrinks because the game was played.
    snapshot = {"SCO": 1700.0, "ENG": 1700.0}
    post = _post(snapshot, [FinishedGame(1, "SCO", "ENG", 2, 2)])
    assert post["SCO"].rating == 1700.0
    assert post["ENG"].rating == 1700.0
    assert post["SCO"].sigma < _SIGMA0


def test_rating_mass_is_conserved():
    # The update is zero-sum, so total rating across all teams is invariant no
    # matter how many games are folded in.
    snapshot = {"A": 1800.0, "B": 1600.0, "C": 1700.0, "D": 1500.0}
    finished = [
        FinishedGame(1, "A", "B", 3, 0),
        FinishedGame(2, "C", "D", 1, 2),
        FinishedGame(3, "A", "C", 0, 0),
    ]
    post = _post(snapshot, finished)
    before = sum(snapshot.values())
    after = sum(ts.rating for ts in post.values())
    assert math.isclose(before, after)


def test_bigger_win_moves_rating_more():
    # The goal-difference multiplier means a 3-0 result shifts the rating by
    # strictly more than a 1-0 result between the same equal-rated teams.
    snapshot = {"SCO": 1700.0, "ENG": 1700.0}
    narrow = _post(snapshot, [FinishedGame(1, "SCO", "ENG", 1, 0)])
    wide = _post(snapshot, [FinishedGame(1, "SCO", "ENG", 3, 0)])
    assert wide["SCO"].rating - 1700.0 > narrow["SCO"].rating - 1700.0


def test_underdog_win_moves_more_than_favourite_win():
    # A weaker team beating a stronger one is more surprising (larger
    # score - expectancy gap), so its rating gain exceeds a favourite's for the
    # same 1-0 scoreline.
    upset = _post({"DOG": 1500.0, "FAV": 1900.0}, [FinishedGame(1, "DOG", "FAV", 1, 0)])
    expected = _post(
        {"FAV": 1900.0, "DOG": 1500.0}, [FinishedGame(1, "FAV", "DOG", 1, 0)]
    )
    assert (upset["DOG"].rating - 1500.0) > (expected["FAV"].rating - 1900.0)


def test_game_with_unknown_code_is_skipped():
    # A finished game referencing a team absent from the snapshot is ignored: no
    # crash, no rating move, and the known team's sigma is NOT shrunk (it has no
    # usable game).
    snapshot = {"SCO": 1700.0}
    post = _post(snapshot, [FinishedGame(1, "SCO", "ZZZ", 2, 0)])
    assert post["SCO"].rating == 1700.0
    assert post["SCO"].sigma == _SIGMA0
    assert "ZZZ" not in post


def test_matchday_order_is_applied_independent_of_input_order():
    # Elo updates are path-dependent, so the function must replay in matchday
    # order. Passing the same games shuffled must yield an identical posterior.
    snapshot = {"A": 1800.0, "B": 1600.0, "C": 1700.0}
    games = [
        FinishedGame(1, "A", "B", 2, 0),
        FinishedGame(2, "B", "C", 1, 1),
        FinishedGame(3, "A", "C", 0, 1),
    ]
    in_order = _post(snapshot, games)
    shuffled = _post(snapshot, [games[2], games[0], games[1]])
    for code in snapshot:
        assert math.isclose(in_order[code].rating, shuffled[code].rating)
