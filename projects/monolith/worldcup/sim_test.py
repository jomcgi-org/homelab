"""Deterministic synthetic scenarios for the WC2026 Monte Carlo.

Each helper builds a small, internally consistent set of TeamState +
Fixture objects plus an elo dict, designed so the asserted answer follows
analytically from the construction (independent of the rng seed where the
docstring says "guaranteed").
"""

import pytest

from worldcup.sim import (
    NEAR_CERTAIN_THRESHOLD,
    NEAR_ELIMINATED_THRESHOLD,
    Fixture,
    TeamState,
    _status,
    simulate,
)

# A neutral baseline Elo used wherever the exact strength does not change the
# logical outcome of a scenario.
_BASE = 1700.0


def _team(code, group, pts, gf, ga, team_id=None):
    return TeamState(
        team_id=team_id or f"id-{code}",
        fifa_code=code,
        group=group,
        pts=pts,
        gf=gf,
        ga=ga,
    )


def _scenario_clinched():
    """Single group of 4. SCO has WON all three of its games (9 pts, max
    possible) and has no remaining fixtures. Every other team has already
    LOST to SCO, so the best any of them can finish is 0 (vs SCO) + 3 + 3 = 6
    points, strictly below 9. SCO is therefore guaranteed 1st place (top 2)
    in every simulation, regardless of how the remaining ENG/WAL/NIR games
    fall and regardless of any rng tiebreak (SCO is never tied). So
    prob_qualify == 1.0 and prob_top2 == 1.0.
    """
    states = [
        _team("SCO", "A", pts=9, gf=9, ga=0),  # beat all three, done
        _team("ENG", "A", pts=0, gf=0, ga=3),  # lost to SCO, 2 games left
        _team("WAL", "A", pts=0, gf=0, ga=3),  # lost to SCO, 2 games left
        _team("NIR", "A", pts=0, gf=0, ga=3),  # lost to SCO, 2 games left
    ]
    # Remaining games are ONLY among the other three teams; none touch SCO.
    fixtures = [
        Fixture("A-ENG-WAL", "A", "ENG", "WAL"),
        Fixture("A-ENG-NIR", "A", "ENG", "NIR"),
        Fixture("A-WAL-NIR", "A", "WAL", "NIR"),
    ]
    elo = {"SCO": _BASE, "ENG": _BASE, "WAL": _BASE, "NIR": _BASE}
    return states, fixtures, elo


def _scenario_eliminated():
    """Single group of 4, fully decided (no remaining fixtures). SCO lost all
    three games: 0 points, finishing strictly below ENG (6), WAL (4) and
    NIR (3). SCO is therefore guaranteed LAST (4th). In a one-group sim the
    thirds pool holds a single team (the 3rd-placed NIR), so only 4th place
    is excluded from qualification. SCO is 4th in every sim, so
    prob_qualify == 0.0 and status == "eliminated". Deterministic: with no
    fixtures nothing is sampled and the standings never move.
    """
    states = [
        _team("ENG", "A", pts=6, gf=8, ga=2),
        _team("WAL", "A", pts=4, gf=5, ga=4),
        _team("NIR", "A", pts=3, gf=4, ga=5),
        _team("SCO", "A", pts=0, gf=0, ga=9),  # lost everything, last
    ]
    fixtures = []  # group is finished
    elo = {"SCO": _BASE, "ENG": _BASE, "WAL": _BASE, "NIR": _BASE}
    return states, fixtures, elo


def _scenario_realistic():
    """Three groups of four with a handful of unfinished fixtures, including
    one that involves SCO (group A). Strengths are varied but the exact
    qualification probability is not asserted; this scenario only checks the
    invariants that must hold for ANY input: every probability sits in
    [0, 1] and the two qualification routes partition the qualify count
    (prob_qualify == prob_top2 + prob_third per team).
    """
    states = [
        # Group A: SCO genuinely on the bubble.
        _team("SCO", "A", pts=3, gf=3, ga=2),
        _team("FRA", "A", pts=6, gf=5, ga=1),
        _team("GER", "A", pts=3, gf=2, ga=2),
        _team("AND", "A", pts=0, gf=0, ga=5),
        # Group B.
        _team("BRA", "B", pts=6, gf=6, ga=1),
        _team("ARG", "B", pts=4, gf=4, ga=2),
        _team("CHI", "B", pts=1, gf=2, ga=4),
        _team("PER", "B", pts=1, gf=1, ga=6),
        # Group C.
        _team("ESP", "C", pts=4, gf=4, ga=2),
        _team("ITA", "C", pts=4, gf=3, ga=2),
        _team("POR", "C", pts=3, gf=3, ga=3),
        _team("MLT", "C", pts=0, gf=1, ga=4),
    ]
    fixtures = [
        Fixture("A-SCO-GER", "A", "SCO", "GER", is_own=True),
        Fixture("A-FRA-AND", "A", "FRA", "AND"),
        Fixture("B-ARG-CHI", "B", "ARG", "CHI"),
        Fixture("C-POR-MLT", "C", "POR", "MLT"),
        Fixture("C-ESP-ITA", "C", "ESP", "ITA"),
    ]
    elo = {
        "SCO": 1700.0,
        "FRA": 1980.0,
        "GER": 1900.0,
        "AND": 1300.0,
        "BRA": 2000.0,
        "ARG": 1990.0,
        "CHI": 1650.0,
        "PER": 1600.0,
        "ESP": 1960.0,
        "ITA": 1900.0,
        "POR": 1930.0,
        "MLT": 1350.0,
    }
    return states, fixtures, elo


def _scenario_swing():
    """Build a scenario where SCO's own last match is decisive AND finishing
    3rd does NOT rescue SCO, so the own match nearly fully determines
    qualification.

    Group A (SCO's group): FRA has 6 points and is effectively 1st. SCO and
    GER are level on 3. Their head-to-head (SCO at home vs GER) is the only
    remaining fixture in the whole tournament:
      - SCO win  -> SCO 6 pts, 2nd, qualifies via the top-2 route.
      - SCO loss -> GER 6 pts (2nd); SCO stays on 3 as the group's 3rd team.

    To make 3rd place worthless for SCO we add eight extra groups that are
    already FINISHED (no fixtures). Each contributes exactly one 3rd-placed
    team holding 4 points, which is strictly more than SCO's 3 points in the
    loss case. The thirds pool then has nine teams and only the best eight
    qualify, so a 3-point SCO ranks ninth and is excluded. Net effect:
    P(qualify | SCO win) ~ 1 and P(qualify | SCO loss) ~ 0, giving a large
    positive swing on the single own match, which is the only fixture and so
    is necessarily swings[0].
    """
    states = [
        _team("FRA", "A", pts=6, gf=6, ga=1),  # 1st locked
        _team("SCO", "A", pts=3, gf=2, ga=2),  # decisive match ahead
        _team("GER", "A", pts=3, gf=2, ga=2),
        _team("AND", "A", pts=0, gf=0, ga=5),  # last, finished its games
    ]
    # Eight finished filler groups, each yielding one 3rd-placed team on 4 pts
    # (> SCO's 3), so SCO-as-third never makes the top-8 thirds cut.
    for i in range(8):
        g = f"F{i}"
        states += [
            _team(f"{g}1", g, pts=9, gf=7, ga=1),
            _team(f"{g}2", g, pts=6, gf=5, ga=2),
            _team(f"{g}3", g, pts=4, gf=4, ga=3),  # the qualifying-strength 3rd
            _team(f"{g}4", g, pts=0, gf=1, ga=8),
        ]
    fixtures = [Fixture("A-SCO-GER", "A", "SCO", "GER", is_own=True)]
    elo = {"SCO": 1750.0, "GER": 1750.0, "FRA": 1900.0, "AND": 1300.0}
    return states, fixtures, elo


@pytest.mark.parametrize(
    "pq,expected",
    [
        # Exact combinatorial certainties.
        (1.0, "qualified"),
        (0.0, "eliminated"),
        # Near_* tiers: overwhelmingly likely but not provably certain. The
        # boundary itself belongs to the near_* tier (>= / <=).
        (NEAR_CERTAIN_THRESHOLD, "near_certain"),
        (0.9995, "near_certain"),
        (0.99999, "near_certain"),
        (NEAR_ELIMINATED_THRESHOLD, "near_eliminated"),
        (0.0005, "near_eliminated"),
        (0.00001, "near_eliminated"),
        # Live contention sits strictly between the thresholds. The values just
        # inside each boundary must NOT be swallowed by a near_* tier.
        (0.99, "contention"),
        (0.9989, "contention"),
        (0.5, "contention"),
        (0.0011, "contention"),
        (0.01, "contention"),
    ],
)
def test_status_tiers(pq, expected):
    assert _status(pq) == expected


def test_clinched_team_is_certain():
    states, fixtures, elo = _scenario_clinched()
    res = simulate(states, fixtures, elo, focus="SCO", n=2000, seed=11)
    sco = res.per_team["SCO"]
    assert sco.prob_qualify == 1.0
    assert sco.prob_top2 == 1.0
    assert sco.prob_third == 0.0
    assert sco.status == "qualified"


def test_eliminated_team_is_zero():
    states, fixtures, elo = _scenario_eliminated()
    res = simulate(states, fixtures, elo, focus="SCO", n=2000, seed=22)
    sco = res.per_team["SCO"]
    assert sco.prob_qualify == 0.0
    assert sco.status == "eliminated"


def test_probabilities_in_unit_interval_and_route_split():
    states, fixtures, elo = _scenario_realistic()
    res = simulate(states, fixtures, elo, focus="SCO", n=4000, seed=33)
    for tp in res.per_team.values():
        assert 0.0 <= tp.prob_qualify <= 1.0
        assert 0.0 <= tp.prob_top2 <= 1.0
        assert 0.0 <= tp.prob_third <= 1.0
        # The two routes are mutually exclusive per simulation.
        assert abs(tp.prob_qualify - (tp.prob_top2 + tp.prob_third)) < 1e-9


def test_swing_identifies_own_match_as_high_impact():
    states, fixtures, elo = _scenario_swing()
    res = simulate(states, fixtures, elo, focus="SCO", n=4000, seed=44)

    own = [s for s in res.swings if s.is_own_match]
    assert own, "expected at least one own-match swing"
    own_swing = own[0]
    assert own_swing.swing > 0.0
    # Winning should help SCO far more than losing in this construction.
    assert own_swing.p_qualify_home_win > own_swing.p_qualify_away_win
    # swings are sorted descending, so the top entry dominates the own match.
    assert res.swings[0].swing >= own_swing.swing


def _scenario_open_group():
    """One group of four, every team level on zero points with all six
    round-robin fixtures still to play, so qualification is driven purely by the
    sampled scorelines. FAV is much stronger than the three peers, which is the
    setup the epistemic-uncertainty tests perturb.
    """
    states = [
        _team("FAV", "A", pts=0, gf=0, ga=0),
        _team("P2", "A", pts=0, gf=0, ga=0),
        _team("P3", "A", pts=0, gf=0, ga=0),
        _team("P4", "A", pts=0, gf=0, ga=0),
    ]
    fixtures = [
        Fixture("A-FAV-P2", "A", "FAV", "P2"),
        Fixture("A-FAV-P3", "A", "FAV", "P3"),
        Fixture("A-FAV-P4", "A", "FAV", "P4"),
        Fixture("A-P2-P3", "A", "P2", "P3"),
        Fixture("A-P2-P4", "A", "P2", "P4"),
        Fixture("A-P3-P4", "A", "P3", "P4"),
    ]
    elo = {"FAV": 2100.0, "P2": 1600.0, "P3": 1600.0, "P4": 1600.0}
    return states, fixtures, elo


def test_sigma_preserves_probability_invariants():
    # The new uncertainty path must still produce valid probabilities: every
    # value in [0, 1] and the two qualification routes partitioning the qualify
    # count, exactly as the deterministic path does.
    states, fixtures, elo = _scenario_open_group()
    sigma = {c: 60.0 for c in elo}
    res = simulate(states, fixtures, elo, focus="FAV", n=4000, seed=7, sigma=sigma)
    for tp in res.per_team.values():
        assert 0.0 <= tp.prob_qualify <= 1.0
        assert abs(tp.prob_qualify - (tp.prob_top2 + tp.prob_third)) < 1e-9


def test_epistemic_uncertainty_regularises_the_favourite():
    # Injecting strength uncertainty pulls outcomes toward the coin-flip: a
    # heavy favourite qualifies LESS often than under a point estimate (its win
    # rate was saturating), and a weaker peer correspondingly MORE often.
    states, fixtures, elo = _scenario_open_group()
    point = simulate(states, fixtures, elo, focus="FAV", n=8000, seed=5)
    wide = simulate(
        states,
        fixtures,
        elo,
        focus="FAV",
        n=8000,
        seed=5,
        sigma={c: 500.0 for c in elo},
    )
    assert wide.per_team["FAV"].prob_top2 < point.per_team["FAV"].prob_top2
    assert wide.per_team["P2"].prob_top2 > point.per_team["P2"].prob_top2


def test_swings_are_computed_per_country():
    # Every team gets its own ranked swing list, and a bubble team's swing on its
    # own matches dwarfs a near-certain favourite's (whose fate barely moves).
    states, fixtures, elo = _scenario_open_group()
    res = simulate(states, fixtures, elo, focus="FAV", n=6000, seed=9, swing_n=3000)
    assert set(res.swings_by_country) == {"FAV", "P2", "P3", "P4"}
    # The back-compat .swings field is exactly the focus team's list.
    assert res.swings is res.swings_by_country["FAV"]
    fav_top = res.swings_by_country["FAV"][0].swing
    p2_top = res.swings_by_country["P2"][0].swing
    assert p2_top > fav_top
    # A team's own matches are flagged in its own list.
    assert any(s.is_own_match for s in res.swings_by_country["P2"])


def test_swing_n_zero_yields_no_swing():
    # With no trials fed to the swing tally every conditional falls back to the
    # unconditional qualify prob, so swing is 0 everywhere; qualification, which
    # always uses all n trials, is unaffected.
    states, fixtures, elo = _scenario_open_group()
    res = simulate(states, fixtures, elo, focus="FAV", n=3000, seed=9, swing_n=0)
    assert all(s.swing == 0.0 for s in res.swings_by_country["FAV"])
    assert 0.0 <= res.per_team["FAV"].prob_qualify <= 1.0


def _scenario_settled_match():
    """A group of four after two matchdays: AAA and BBB have both won twice
    (6 pts) while CCC and DDD have lost twice (0 pts). The matchday-three
    fixtures are AAA vs BBB and CCC vs DDD.

    AAA and BBB each finish on >= 6 points whatever happens between them, while
    the 0-point teams can reach at most 3, so both are guaranteed top-two and
    are "qualified". Their head-to-head is therefore a settled-vs-settled match
    that can move no team and must be dropped from every swing list. CCC and DDD
    are still contesting the single third-place slot, so their match is the one
    decisive fixture and is kept.
    """
    states = [
        _team("AAA", "A", pts=6, gf=6, ga=0),
        _team("BBB", "A", pts=6, gf=6, ga=0),
        _team("CCC", "A", pts=0, gf=0, ga=3),
        _team("DDD", "A", pts=0, gf=0, ga=3),
    ]
    fixtures = [
        Fixture("A-AAA-BBB", "A", "AAA", "BBB"),
        Fixture("A-CCC-DDD", "A", "CCC", "DDD"),
    ]
    elo = {c: _BASE for c in ("AAA", "BBB", "CCC", "DDD")}
    return states, fixtures, elo


def test_settled_vs_settled_match_excluded_from_swings():
    # A match between two already-qualified teams has a true swing of 0 for every
    # team, so it is dropped from all swing lists rather than surfaced with a noisy
    # estimate; the still-live third-place match survives.
    states, fixtures, elo = _scenario_settled_match()
    res = simulate(states, fixtures, elo, focus="AAA", n=4000, seed=3, swing_n=4000)
    assert res.per_team["AAA"].status == "qualified"
    assert res.per_team["BBB"].status == "qualified"
    # The settled-vs-settled fixture is absent from every team's swing list.
    for code, rows in res.swings_by_country.items():
        assert all(s.match_id != "A-AAA-BBB" for s in rows), code
    # The genuinely decisive third-place match is still present.
    assert "A-CCC-DDD" in {s.match_id for s in res.swings_by_country["CCC"]}


def _scenario_head_to_head_tie():
    """A finished group of four where SCO and BRA are level on points for 2nd.

    BRA carries a much bigger overall goal difference (+5 vs -1), but SCO won
    the head-to-head meeting. The group is fully played out (no remaining
    fixtures) so the result is deterministic: only the tiebreaker decides who
    takes 2nd (top-two) and who drops to the third-place pool.
    """
    states = [
        _team("FRA", "A", pts=9, gf=9, ga=1),  # clear 1st
        _team("BRA", "A", pts=4, gf=6, ga=1),  # overall GD +5
        _team("SCO", "A", pts=4, gf=3, ga=4),  # overall GD -1, but beat BRA
        _team("AND", "A", pts=0, gf=0, ga=12),  # clear 4th
    ]
    fixtures = []  # group already complete
    elo = {c: _BASE for c in ("FRA", "BRA", "SCO", "AND")}
    # SCO won the head-to-head 2-1 (BRA at home, SCO away).
    finished = [("BRA", "SCO", 1, 2)]
    return states, fixtures, elo, finished


def test_head_to_head_breaks_tie_for_second():
    # FIFA 2026 Article 13: head-to-head outranks overall goal difference, so
    # SCO (who beat BRA) takes 2nd despite the worse overall GD, and BRA drops
    # to the third-place pool. Deterministic, so any n and seed agree.
    states, fixtures, elo, finished = _scenario_head_to_head_tie()
    res = simulate(
        states, fixtures, elo, focus="SCO", n=50, seed=1, finished_results=finished
    )
    assert res.per_team["SCO"].prob_top2 == 1.0
    assert res.per_team["BRA"].prob_top2 == 0.0


def test_without_head_to_head_overall_gd_decides():
    # Control: with no head-to-head result supplied, the tie falls through to
    # overall goal difference, so BRA's larger GD takes 2nd instead. This guards
    # the mechanism: the only difference from the test above is finished_results.
    states, fixtures, elo, _finished = _scenario_head_to_head_tie()
    res = simulate(
        states, fixtures, elo, focus="SCO", n=50, seed=1, finished_results=None
    )
    assert res.per_team["BRA"].prob_top2 == 1.0
    assert res.per_team["SCO"].prob_top2 == 0.0


def test_attack_defence_factors_neutral_without_games():
    # The TeamState default gp=0 must leave every attack/defence factor at 1.0
    # so a sim built from games-less states is identical to the plain Elo split.
    from worldcup.sim import attack_defence

    states, _fixtures, elo = _scenario_open_group()
    factors = attack_defence(states)
    assert set(factors) == set(elo)
    for att, dfn in factors.values():
        assert att == 1.0 and dfn == 1.0


def test_attack_defence_reflects_observed_scoring():
    # A team that has scored freely and conceded little earns attack > 1 and
    # defence < 1; a leaky low-scoring team is the mirror image.
    from worldcup.sim import attack_defence

    states = [
        _team("HOT", "A", pts=6, gf=8, ga=1, team_id="id-HOT"),  # 2 games, prolific
        _team("LEK", "A", pts=0, gf=1, ga=8, team_id="id-LEK"),  # 2 games, leaky
    ]
    states[0].gp = 2
    states[1].gp = 2
    factors = attack_defence(states)
    hot_att, hot_def = factors["HOT"]
    lek_att, lek_def = factors["LEK"]
    assert hot_att > 1.0 and hot_def < 1.0
    assert lek_att < 1.0 and lek_def > 1.0
