"""Monte Carlo simulation of WC2026 group-stage qualification."""

from __future__ import annotations

import random
from dataclasses import dataclass

from worldcup.outcome import sample_scoreline


@dataclass
class TeamState:
    team_id: str
    fifa_code: str
    group: str
    pts: int
    gf: int
    ga: int

    @property
    def gd(self) -> int:
        return self.gf - self.ga


@dataclass
class Fixture:
    match_id: str
    group: str
    home_code: str
    away_code: str
    is_own: bool = False  # involves the focus team


@dataclass
class TeamProb:
    fifa_code: str
    prob_qualify: float
    prob_top2: float
    prob_third: float
    status: str


@dataclass
class Swing:
    match_id: str
    group: str
    home_code: str
    away_code: str
    swing: float
    p_qualify_home_win: float
    p_qualify_draw: float
    p_qualify_away_win: float
    is_own_match: bool


@dataclass
class SimResult:
    per_team: dict[str, TeamProb]  # keyed by fifa_code
    swings: list[Swing]  # sorted by swing desc
    n: int


def simulate(states, fixtures, elo, focus, n=20000, seed=None) -> SimResult:
    rng = random.Random(seed)
    by_code = {s.fifa_code: s for s in states}
    qualify_count = {s.fifa_code: 0 for s in states}
    top2_count = {s.fifa_code: 0 for s in states}
    third_count = {s.fifa_code: 0 for s in states}

    swing_tally = {
        f.match_id: {"home_win": [0, 0], "draw": [0, 0], "away_win": [0, 0]}
        for f in fixtures
    }  # each value = [focus_qualified, total]

    # precompute group membership
    group_codes = {}
    for s in states:
        group_codes.setdefault(s.group, []).append(s.fifa_code)

    for _ in range(n):
        acc = {c: [s.pts, s.gf, s.ga] for c, s in by_code.items()}  # [pts, gf, ga]
        outcomes = {}
        for f in fixtures:
            h, a = sample_scoreline(elo[f.home_code], elo[f.away_code], rng)
            acc[f.home_code][1] += h
            acc[f.home_code][2] += a
            acc[f.away_code][1] += a
            acc[f.away_code][2] += h
            if h > a:
                acc[f.home_code][0] += 3
                outcomes[f.match_id] = "home_win"
            elif h == a:
                acc[f.home_code][0] += 1
                acc[f.away_code][0] += 1
                outcomes[f.match_id] = "draw"
            else:
                acc[f.away_code][0] += 3
                outcomes[f.match_id] = "away_win"

        qualified = set()
        thirds = []
        for g, codes in group_codes.items():
            ranked = sorted(
                codes,
                key=lambda c: (
                    acc[c][0],
                    acc[c][1] - acc[c][2],
                    acc[c][1],
                    rng.random(),
                ),
                reverse=True,
            )
            qualified.update(ranked[:2])
            for c in ranked[:2]:
                top2_count[c] += 1
            if len(ranked) >= 3:
                thirds.append(ranked[2])

        thirds_ranked = sorted(
            thirds,
            key=lambda c: (acc[c][0], acc[c][1] - acc[c][2], acc[c][1], rng.random()),
            reverse=True,
        )
        third_qualifiers = set(thirds_ranked[:8])
        qualified |= third_qualifiers
        for c in third_qualifiers:
            third_count[c] += 1

        for c in qualified:
            qualify_count[c] += 1

        focus_q = focus in qualified
        for mid, oc in outcomes.items():
            cell = swing_tally[mid][oc]
            cell[1] += 1
            if focus_q:
                cell[0] += 1

    per_team = {}
    for c in by_code:
        pq = qualify_count[c] / n
        per_team[c] = TeamProb(
            fifa_code=c,
            prob_qualify=pq,
            prob_top2=top2_count[c] / n,
            prob_third=third_count[c] / n,
            status="qualified"
            if pq == 1.0
            else "eliminated"
            if pq == 0.0
            else "contention",
        )

    focus_prob = per_team[focus].prob_qualify if focus in per_team else 0.0
    swings = []
    for f in fixtures:
        conds = {}
        for oc in ("home_win", "draw", "away_win"):
            q, t = swing_tally[f.match_id][oc]
            conds[oc] = (q / t) if t else focus_prob
        swing = max(conds.values()) - min(conds.values())
        swings.append(
            Swing(
                match_id=f.match_id,
                group=f.group,
                home_code=f.home_code,
                away_code=f.away_code,
                swing=swing,
                p_qualify_home_win=conds["home_win"],
                p_qualify_draw=conds["draw"],
                p_qualify_away_win=conds["away_win"],
                is_own_match=f.is_own,
            )
        )
    swings.sort(key=lambda s: s.swing, reverse=True)
    return SimResult(per_team=per_team, swings=swings, n=n)
