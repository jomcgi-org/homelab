import random

from worldcup.outcome import outcome_probabilities, sample_scoreline, win_expectancy


def test_win_expectancy_symmetry():
    assert abs(win_expectancy(1700, 1700) - 0.5) < 1e-9
    assert win_expectancy(1900, 1700) > 0.5
    assert win_expectancy(1500, 1700) < 0.5


def test_equal_teams_draw_rate_realistic():
    probs = outcome_probabilities(1700, 1700, rng=random.Random(42), n=20000)
    assert abs(probs["home_win"] - probs["away_win"]) < 0.03
    assert 0.20 < probs["draw"] < 0.30


def test_stronger_team_wins_more():
    probs = outcome_probabilities(1950, 1600, rng=random.Random(7), n=20000)
    assert probs["home_win"] > 0.55
    assert probs["home_win"] > probs["away_win"]


def test_scoreline_is_nonnegative_ints():
    h, a = sample_scoreline(1800, 1500, random.Random(1))
    assert isinstance(h, int) and isinstance(a, int)
    assert h >= 0 and a >= 0
