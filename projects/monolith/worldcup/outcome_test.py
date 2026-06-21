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


def _mean_scoreline(n, **kwargs):
    rng = random.Random(99)
    hs = as_ = 0
    for _ in range(n):
        h, a = sample_scoreline(1700, 1700, rng, **kwargs)
        hs += h
        as_ += a
    return hs / n, as_ / n


def test_attack_factor_lifts_own_goals():
    # A bigger home attack factor raises the home team's expected goals while
    # leaving the away team's rate untouched (the second dimension the single
    # Elo scalar could not express).
    base_h, base_a = _mean_scoreline(20000)
    boosted_h, boosted_a = _mean_scoreline(20000, att_home=1.5)
    assert boosted_h > base_h * 1.2
    assert abs(boosted_a - base_a) < 0.1


def test_defence_factor_lifts_opponent_goals():
    # A leaky home defence (def_home > 1) raises the AWAY team's expected goals:
    # attack and defence move independently.
    base_h, base_a = _mean_scoreline(20000)
    leaky_h, leaky_a = _mean_scoreline(20000, def_home=1.5)
    assert leaky_a > base_a * 1.2
    assert abs(leaky_h - base_h) < 0.1


def test_dixon_coles_lifts_low_score_draw_rate():
    # A negative rho is the Dixon-Coles correction for independent Poisson
    # under-producing 0-0 and 1-1; it should raise the overall draw rate for
    # evenly matched teams relative to the uncorrected model.
    indep = outcome_probabilities(1700, 1700, random.Random(3), n=40000, rho=0.0)
    corrected = outcome_probabilities(1700, 1700, random.Random(3), n=40000, rho=-0.1)
    assert corrected["draw"] > indep["draw"]
    # Still a valid distribution and roughly symmetric (no home bias introduced).
    assert abs(corrected["home_win"] - corrected["away_win"]) < 0.03


def test_rho_default_matches_independent_poisson():
    # With the default rho the sampler must reproduce the independent-Poisson
    # stream byte-for-byte, so existing behaviour is unchanged.
    rng_a = random.Random(123)
    rng_b = random.Random(123)
    for _ in range(500):
        assert sample_scoreline(1850, 1600, rng_a) == sample_scoreline(
            1850, 1600, rng_b, rho=0.0
        )
