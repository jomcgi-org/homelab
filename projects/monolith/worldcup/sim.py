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
    swings: list[Swing]  # the focus team's swings, sorted desc (back-compat)
    n: int
    # Every team's ranked swing list, keyed by fifa_code. swings == the focus
    # team's entry; the page lets a visitor pick any country from this map.
    swings_by_country: dict[str, list[Swing]] | None = None


# Fixed outcome ordering, mapped to integer indices for the hot swing tally.
_OC_IDX = {"home_win": 0, "draw": 1, "away_win": 2}


def simulate(
    states, fixtures, elo, focus, n=20000, seed=None, sigma=None, swing_n=None
) -> SimResult:
    """Monte Carlo over the remaining fixtures.

    ``elo`` maps fifa_code -> rating (the posterior mean strength). ``sigma`` is
    optional: when None the ratings are used as exact point estimates (the
    original deterministic-strength behaviour, byte-identical rng stream). When a
    fifa_code -> std map is given, each team's true strength is treated as
    uncertain: once per trial we draw a single rating ~ Normal(elo[code],
    sigma[code]) and reuse it for ALL of that team's remaining fixtures in that
    trial. The draw is per team per trial (NOT per match) because a team has one
    unknown true strength within a single simulated tournament; drawing per match
    would average the epistemic uncertainty straight back out.

    ``swing_n`` caps how many trials feed the per-country swing tally (None means
    all ``n``). Qualification probabilities always use all ``n`` trials, but the
    swing cross-product (remaining matches x qualified teams) is the expensive
    part, and swing is only a ranking, so a subset (e.g. 100k) keeps it accurate
    while bounding cost.
    """
    rng = random.Random(seed)
    by_code = {s.fifa_code: s for s in states}
    # Teams whose strength is actually sampled: those in a remaining fixture.
    # Precomputed so the per-trial draw loop stays tight.
    fixture_codes = {c for f in fixtures for c in (f.home_code, f.away_code)}
    qualify_count = {s.fifa_code: 0 for s in states}
    top2_count = {s.fifa_code: 0 for s in states}
    third_count = {s.fifa_code: 0 for s in states}

    # Per-country swing tally. code_to_idx maps each team to a column; for every
    # (match, outcome) we keep a total trial count plus a per-team count of how
    # often that team qualified given the outcome. P(team qualifies | outcome) is
    # then count / total, and swing is the spread across the three outcomes.
    codes = list(by_code)
    code_to_idx = {c: i for i, c in enumerate(codes)}
    match_to_idx = {f.match_id: i for i, f in enumerate(fixtures)}
    swing_counts = [[[0] * len(codes) for _ in range(3)] for _ in fixtures]
    swing_totals = [[0, 0, 0] for _ in fixtures]
    swing_cap = n if swing_n is None else min(swing_n, n)

    # precompute group membership
    group_codes = {}
    for s in states:
        group_codes.setdefault(s.group, []).append(s.fifa_code)

    for t in range(n):
        acc = {c: [s.pts, s.gf, s.ga] for c, s in by_code.items()}  # [pts, gf, ga]
        # One strength draw per team per trial (epistemic uncertainty). With no
        # sigma the effective rating is just the point estimate, so the rng is
        # never touched and the deterministic path is byte-identical to before.
        if sigma:
            strength = {c: rng.gauss(elo[c], sigma.get(c, 0.0)) for c in fixture_codes}
        else:
            strength = elo
        outcomes = {}
        for f in fixtures:
            h, a = sample_scoreline(strength[f.home_code], strength[f.away_code], rng)
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

        # Per-country swing tally over the first swing_cap trials only: for each
        # match's outcome this trial, bump the total and, for every team that
        # qualified, its count. Iterating the qualified set (~32 of 48) keeps the
        # cross-product to qualified-teams x remaining-matches per trial.
        if t < swing_cap:
            qidx = [code_to_idx[c] for c in qualified]
            for mid, oc in outcomes.items():
                m = match_to_idx[mid]
                o = _OC_IDX[oc]
                swing_totals[m][o] += 1
                cm = swing_counts[m][o]
                for ci in qidx:
                    cm[ci] += 1

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

    # Build each country's ranked swing list from the tally. When an outcome was
    # never sampled (totals 0, e.g. swing_n == 0 or an impossible result) fall
    # back to the team's unconditional qualify probability so its swing is 0.
    swings_by_country: dict[str, list[Swing]] = {}
    for c in by_code:
        ci = code_to_idx[c]
        cprob = per_team[c].prob_qualify
        rows = []
        for f in fixtures:
            m = match_to_idx[f.match_id]
            conds = {}
            for oc, o in _OC_IDX.items():
                tot = swing_totals[m][o]
                conds[oc] = (swing_counts[m][o][ci] / tot) if tot else cprob
            swing = max(conds.values()) - min(conds.values())
            rows.append(
                Swing(
                    match_id=f.match_id,
                    group=f.group,
                    home_code=f.home_code,
                    away_code=f.away_code,
                    swing=swing,
                    p_qualify_home_win=conds["home_win"],
                    p_qualify_draw=conds["draw"],
                    p_qualify_away_win=conds["away_win"],
                    is_own_match=c in (f.home_code, f.away_code),
                )
            )
        rows.sort(key=lambda s: s.swing, reverse=True)
        swings_by_country[c] = rows

    swings = swings_by_country.get(focus, [])
    return SimResult(
        per_team=per_team,
        swings=swings,
        n=n,
        swings_by_country=swings_by_country,
    )
