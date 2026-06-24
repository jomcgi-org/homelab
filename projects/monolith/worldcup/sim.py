"""Monte Carlo simulation of WC2026 group-stage qualification."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

from worldcup.outcome import AVG_TOTAL, sample_scoreline

# Pseudo-count for shrinking a team's observed scoring toward the tournament
# average when forming its attack/defence factors. With two games played a
# team's factor sits roughly halfway between the neutral 1.0 and its raw
# per-game rate, reflecting how little two results actually tell us.
ATT_DEF_SHRINK = float(os.environ.get("WORLDCUP_ATTDEF_SHRINK", "2"))


@dataclass
class TeamState:
    team_id: str
    fifa_code: str
    group: str
    pts: int
    gf: int
    ga: int
    gp: int = 0  # games played, drives the attack/defence shrink (0 -> neutral)

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

# A team is "qualified"/"eliminated" only when it advances/fails in EVERY trial
# (an exact combinatorial certainty the Monte Carlo reproduces as pq == 1.0/0.0).
# Between those extremes and the certainties sit two "effectively settled" tiers:
# at or beyond these thresholds no realistic combination of remaining results
# changes the outcome, so we label them distinctly and drop their swing cards
# (which would otherwise all read a rounded 100%/0%). The thresholds also bound
# what "contention" can be, so a contending team's headline never rounds to a
# misleading 100%/0% (the page clamps the contention figure to 1..99%).
NEAR_CERTAIN_THRESHOLD = 0.999
NEAR_ELIMINATED_THRESHOLD = 0.001


def _status(pq: float) -> str:
    """Map a qualify probability to a display status tier.

    Exact 1.0/0.0 are combinatorial certainties ("qualified"/"eliminated"). The
    near_* tiers are "all but settled": overwhelmingly likely but not provably
    certain. Everything else is live "contention".
    """
    if pq >= 1.0:
        return "qualified"
    if pq <= 0.0:
        return "eliminated"
    if pq >= NEAR_CERTAIN_THRESHOLD:
        return "near_certain"
    if pq <= NEAR_ELIMINATED_THRESHOLD:
        return "near_eliminated"
    return "contention"


def attack_defence(
    states, shrink: float = ATT_DEF_SHRINK
) -> dict[str, tuple[float, float]]:
    """Per-team multiplicative (attack, defence) factors from observed scoring.

    A team that has scored more than the tournament's per-game average earns an
    attack factor > 1 (it scores more); one that has conceded more earns a
    defence factor > 1 (it leaks more, so opponents score more against it). Both
    are shrunk toward 1.0 with a pseudo-count of ``shrink`` games at the league
    rate, so a team with no games played is neutral (1.0, 1.0) and the model
    reduces to the plain Elo split. Returns {fifa_code: (attack, defence)}.
    """
    total_goals = sum(s.gf for s in states if s.gp > 0)
    total_games = sum(s.gp for s in states if s.gp > 0)
    league_rate = (total_goals / total_games) if total_games else (AVG_TOTAL / 2.0)
    if league_rate <= 0.0:
        league_rate = AVG_TOTAL / 2.0
    out: dict[str, tuple[float, float]] = {}
    for s in states:
        att_rate = (s.gf + shrink * league_rate) / (s.gp + shrink)
        def_rate = (s.ga + shrink * league_rate) / (s.gp + shrink)
        out[s.fifa_code] = (att_rate / league_rate, def_rate / league_rate)
    return out


def _break_tie(tied, acc, finished_pairs, sim_pairs, rng):
    """Order teams level on overall points by FIFA's 2026 within-group ladder.

    Article 13 of the 2026 regulations puts head-to-head first: among the tied
    teams, points then goal difference then goals scored in the matches between
    exactly those teams, and only then overall goal difference, overall goals,
    and finally a coin flip standing in for conduct / FIFA ranking. The
    head-to-head mini-table is computed over every match between the tied teams,
    finished or simulated. (For a 2-way tie this is exact; a 3-way tie uses the
    standard single-pass mini-table rather than FIFA's recursive re-application.)
    """
    tied_set = set(tied)
    h2h = {c: [0, 0, 0] for c in tied}  # [pts, gf, ga] in matches among the tied
    for pairs in (finished_pairs, sim_pairs):
        for (x, y), (gx, gy) in pairs.items():
            if x in tied_set and y in tied_set:
                h2h[x][1] += gx
                h2h[x][2] += gy
                h2h[y][1] += gy
                h2h[y][2] += gx
                if gx > gy:
                    h2h[x][0] += 3
                elif gx < gy:
                    h2h[y][0] += 3
                else:
                    h2h[x][0] += 1
                    h2h[y][0] += 1
    return sorted(
        tied,
        key=lambda c: (
            h2h[c][0],
            h2h[c][1] - h2h[c][2],
            h2h[c][1],
            acc[c][1] - acc[c][2],  # overall goal difference
            acc[c][1],  # overall goals scored
            rng.random(),  # conduct / FIFA ranking, unknowable -> coin flip
        ),
        reverse=True,
    )


def _rank_group(codes, acc, finished_pairs, sim_pairs, rng):
    """Rank a group: by overall points, ties broken by the head-to-head ladder."""
    by_pts: dict[int, list[str]] = {}
    for c in codes:
        by_pts.setdefault(acc[c][0], []).append(c)
    ordered: list[str] = []
    for pts in sorted(by_pts, reverse=True):
        tied = by_pts[pts]
        if len(tied) == 1:
            ordered.append(tied[0])
        else:
            ordered.extend(_break_tie(tied, acc, finished_pairs, sim_pairs, rng))
    return ordered


def simulate(
    states,
    fixtures,
    elo,
    focus,
    n=20000,
    seed=None,
    sigma=None,
    swing_n=None,
    rho=0.0,
    finished_results=None,
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

    ``rho`` is the Dixon-Coles low-score correction passed through to the
    scoreline sampler (0 -> independent Poisson). ``finished_results`` is the
    list of already-played group results as ``(home_code, away_code,
    home_score, away_score)`` tuples; together with the simulated remaining
    results they feed the within-group head-to-head tiebreaker. Attack/defence
    factors are derived once from the teams' observed goals (see
    ``attack_defence``) and reused across trials.
    """
    rng = random.Random(seed)
    by_code = {s.fifa_code: s for s in states}
    att_def = attack_defence(states)
    finished_pairs = {(h, a): (hs, as_) for (h, a, hs, as_) in (finished_results or [])}
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
        sim_pairs = {}  # (home_code, away_code) -> (home_goals, away_goals)
        for f in fixtures:
            att_h, def_h = att_def.get(f.home_code, (1.0, 1.0))
            att_a, def_a = att_def.get(f.away_code, (1.0, 1.0))
            h, a = sample_scoreline(
                strength[f.home_code],
                strength[f.away_code],
                rng,
                att_home=att_h,
                def_home=def_h,
                att_away=att_a,
                def_away=def_a,
                rho=rho,
            )
            acc[f.home_code][1] += h
            acc[f.home_code][2] += a
            acc[f.away_code][1] += a
            acc[f.away_code][2] += h
            sim_pairs[(f.home_code, f.away_code)] = (h, a)
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
            ranked = _rank_group(codes, acc, finished_pairs, sim_pairs, rng)
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
            status=_status(pq),
        )

    # Build each country's ranked swing list from the tally. When an outcome was
    # never sampled (totals 0, e.g. swing_n == 0 or an impossible result) fall
    # back to the team's unconditional qualify probability so its swing is 0.
    #
    # A match between two teams whose fates are already settled (each one
    # qualified, eliminated, or in a near_* tier, i.e. status != "contention") is
    # skipped entirely: it cannot move ANY team's qualification.
    # Neither participant's own standing matters anymore, and a settled team is
    # never a best-third contender whose goal difference could shift the cutoff, so
    # the match's true swing is exactly 0 for everyone. We drop it rather than
    # surface its noisy Monte Carlo estimate: a rare upset between two settled teams
    # is sampled in very few trials, so its conditional qualify-probability is high
    # variance and reads as a phantom swing (the Tunisia-Netherlands case).
    settled = {code for code, tp in per_team.items() if tp.status != "contention"}
    swings_by_country: dict[str, list[Swing]] = {}
    for c in by_code:
        ci = code_to_idx[c]
        cprob = per_team[c].prob_qualify
        rows = []
        for f in fixtures:
            if f.home_code in settled and f.away_code in settled:
                continue
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
