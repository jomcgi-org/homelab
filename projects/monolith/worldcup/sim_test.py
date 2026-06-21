"""Deterministic synthetic scenarios for the WC2026 Monte Carlo.

Each helper builds a small, internally consistent set of TeamState +
Fixture objects plus an elo dict, designed so the asserted answer follows
analytically from the construction (independent of the rng seed where the
docstring says "guaranteed").
"""

from worldcup.sim import Fixture, TeamState, simulate

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
