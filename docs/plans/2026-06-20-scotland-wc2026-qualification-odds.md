# Scotland WC2026 Qualification Odds Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** An unlisted public page at `jomcgi.dev/app/wc2026` that shows Scotland's live probability of reaching the World Cup 2026 Round of 32 (overall: top-2 OR best-third), plus the ranked list of remaining matches whose results most swing that probability.

**Architecture:** A new `worldcup` module in the monolith. A private scheduled job polls the free `worldcup26.ir` API (`/get/groups`, `/get/games`), upserts standings + fixtures into a `worldcup` Postgres schema, then runs an Elo-weighted Monte Carlo over ALL remaining group games to compute Scotland's qualification probability and the per-match swings, writing those results back to Postgres. `monolith-public` reads a `public_reader`-granted set of tables (wholly public data, no `public_api` view needed, exactly like `dr_jobs`) and renders a SvelteKit SSR page. The public tier does zero compute and never egresses (it cannot: the Linkerd EgressNetwork Deny policy blocks it), so all fetching and simulation live on the private side.

**Tech Stack:** Python 3 (FastAPI, SQLModel, httpx) for backend/poller/sim; standard-library `random` (seedable) for Monte Carlo (no numpy dependency); SvelteKit SSR for the page; Atlas-managed SQL migrations; Bazel BUILD hand-wiring; apko image; Helm/ArgoCD deploy.

---

## Conventions for this plan (read first)

- **No local test loop.** Per `.claude/CLAUDE.md`, do NOT run `pytest`/`bazel test` from the workstation. Each task writes its test(s) alongside the implementation and commits. The "Expected" blocks describe what the test asserts; **actual execution happens on BuildBuddy CI** after the branch is pushed (final task). Treat a written-and-committed test as the unit of progress.
- **Worktree:** all work happens in `/tmp/claude-worktrees/wc2026-scotland-odds` (already created off `origin/main`).
- **Commit style:** Conventional Commits, scope `worldcup` (e.g. `feat(worldcup): add Elo match-outcome model`). No em-dashes anywhere.
- **Test fixtures use SQLite + `SQLModel.metadata.create_all`, not migrations** (see `projects/monolith/CLAUDE.md`). Mirror any CHECK constraints in `__table_args__`. `TIMESTAMPTZ` round-trips naive in SQLite, so use a `_as_utc` helper when serializing (copy the one in `dr_jobs/router.py`).
- **The `worldcup` dir is gazelle-excluded** like every other monolith domain, so every new file must be hand-added to BUILD globs (Task 9). Do not rely on `format`/gazelle to register them.

---

## Module layout (target)

```
projects/monolith/worldcup/
  __init__.py          # register(app) / register_public(app) / on_startup_jobs(session)
  models.py            # SQLModel tables (schema="worldcup")
  ratings.py           # load committed Elo snapshot
  ratings/elo_2026.json# static Elo snapshot of all 48 teams (committed data)
  outcome.py           # Elo -> per-match scoreline sampler (the probability model)
  sim.py               # Monte Carlo qualification simulation + swing computation
  client.py            # worldcup26.ir API client (httpx, pure fetch/parse)
  jobs.py              # scheduled handler: poll -> upsert -> simulate -> store
  router.py            # SSR-only read API: GET /api/wc2026/*
  outcome_test.py
  sim_test.py
  client_test.py
  router_test.py

projects/monolith/chart/migrations/
  20260620120000_worldcup_schema.sql
  20260620120100_worldcup_public_reader_grant.sql

projects/monolith/frontend/src/routes/public/app/wc2026/
  +page.server.js
  +page.svelte
```

---

### Task 1: Schema migrations + SQLModel tables

**Files:**

- Create: `projects/monolith/chart/migrations/20260620120000_worldcup_schema.sql`
- Create: `projects/monolith/chart/migrations/20260620120100_worldcup_public_reader_grant.sql`
- Create: `projects/monolith/worldcup/__init__.py` (empty package marker for now)
- Create: `projects/monolith/worldcup/models.py`

**Step 1: Write the schema migration**

`20260620120000_worldcup_schema.sql`:

```sql
-- World Cup 2026 Scotland qualification tracker.
-- Standings + fixtures mirrored from worldcup26.ir; sim outputs computed in-cluster.
-- Wholly public data: granted directly to public_reader (see the grant migration),
-- no public_api view needed (mirrors dr_jobs).
CREATE SCHEMA IF NOT EXISTS worldcup;

-- One row per team per group, refreshed each poll.
CREATE TABLE worldcup.standings (
    team_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    fifa_code   TEXT NOT NULL,
    flag_url    TEXT,
    group_name  TEXT NOT NULL,
    mp          INTEGER NOT NULL DEFAULT 0,
    w           INTEGER NOT NULL DEFAULT 0,
    d           INTEGER NOT NULL DEFAULT 0,
    l           INTEGER NOT NULL DEFAULT 0,
    pts         INTEGER NOT NULL DEFAULT 0,
    gf          INTEGER NOT NULL DEFAULT 0,
    ga          INTEGER NOT NULL DEFAULT 0,
    gd          INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worldcup_standings_group ON worldcup.standings (group_name);

-- One row per group-stage fixture.
CREATE TABLE worldcup.fixtures (
    match_id    TEXT PRIMARY KEY,
    group_name  TEXT NOT NULL,
    matchday    INTEGER NOT NULL,
    home_id     TEXT NOT NULL,
    home_name   TEXT NOT NULL,
    home_code   TEXT NOT NULL,
    away_id     TEXT NOT NULL,
    away_name   TEXT NOT NULL,
    away_code   TEXT NOT NULL,
    home_score  INTEGER,
    away_score  INTEGER,
    finished    BOOLEAN NOT NULL DEFAULT FALSE,
    kickoff     TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worldcup_fixtures_group ON worldcup.fixtures (group_name);

-- Per-team simulation output (we sim all teams; the page reads Scotland's row).
CREATE TABLE worldcup.qualification (
    team_id        TEXT PRIMARY KEY,
    fifa_code      TEXT NOT NULL,
    prob_qualify   DOUBLE PRECISION NOT NULL,
    prob_top2      DOUBLE PRECISION NOT NULL,
    prob_third     DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL DEFAULT 'contention', -- 'qualified' | 'eliminated' | 'contention'
    n_sims         INTEGER NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ranked "matches that could change it" for the focus team (Scotland).
CREATE TABLE worldcup.swing_matches (
    match_id            TEXT PRIMARY KEY,
    focus_team_id       TEXT NOT NULL,
    group_name          TEXT NOT NULL,
    home_code           TEXT NOT NULL,
    away_code           TEXT NOT NULL,
    kickoff             TIMESTAMPTZ,
    swing               DOUBLE PRECISION NOT NULL, -- max-min of conditional qualify prob
    p_qualify_home_win  DOUBLE PRECISION NOT NULL,
    p_qualify_draw      DOUBLE PRECISION NOT NULL,
    p_qualify_away_win  DOUBLE PRECISION NOT NULL,
    is_own_match        BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worldcup_swing_rank ON worldcup.swing_matches (swing DESC);
```

**Step 2: Write the grant migration**

`20260620120100_worldcup_public_reader_grant.sql` (copy the wording from `20260618000000_dr_jobs_public_reader_grant.sql`):

```sql
-- World Cup 2026 data is wholly public. Grant public_reader direct SELECT,
-- joining hikes/ships/stars/dr_jobs as a directly-readable schema (no public_api view).
GRANT USAGE ON SCHEMA worldcup TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA worldcup TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA worldcup GRANT SELECT ON TABLES TO public_reader;
```

**Step 3: Update `atlas.sum`**

The migrations dir is Atlas-managed with a checksum file. Regenerate it so CI's migration-lint passes. From the worktree:

```bash
cd projects/monolith/chart/migrations && atlas migrate hash
```

If `atlas` is not vendored, replicate the existing hash format by following the pattern in the current `atlas.sum` (each line is `h1:` base64 sha of the file). Verify `atlas.sum` lists both new files. (If unsure, leave a note in the commit and let CI's migration check report the exact expected hash, then fix in the CI-iteration task.)

**Step 4: Write `models.py`** mirroring the tables:

```python
"""SQLModel tables for the World Cup 2026 qualification tracker (schema 'worldcup')."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

_SCHEMA = {"schema": "worldcup", "extend_existing": True}


class Standing(SQLModel, table=True):
    __tablename__ = "standings"
    __table_args__ = _SCHEMA
    team_id: str = Field(primary_key=True)
    name: str
    fifa_code: str
    flag_url: str | None = None
    group_name: str
    mp: int = 0
    w: int = 0
    d: int = 0
    l: int = 0
    pts: int = 0
    gf: int = 0
    ga: int = 0
    gd: int = 0
    updated_at: datetime | None = None


class Fixture(SQLModel, table=True):
    __tablename__ = "fixtures"
    __table_args__ = _SCHEMA
    match_id: str = Field(primary_key=True)
    group_name: str
    matchday: int
    home_id: str
    home_name: str
    home_code: str
    away_id: str
    away_name: str
    away_code: str
    home_score: int | None = None
    away_score: int | None = None
    finished: bool = False
    kickoff: datetime | None = None
    updated_at: datetime | None = None


class Qualification(SQLModel, table=True):
    __tablename__ = "qualification"
    __table_args__ = _SCHEMA
    team_id: str = Field(primary_key=True)
    fifa_code: str
    prob_qualify: float
    prob_top2: float
    prob_third: float
    status: str = "contention"
    n_sims: int
    computed_at: datetime | None = None


class SwingMatch(SQLModel, table=True):
    __tablename__ = "swing_matches"
    __table_args__ = _SCHEMA
    match_id: str = Field(primary_key=True)
    focus_team_id: str
    group_name: str
    home_code: str
    away_code: str
    kickoff: datetime | None = None
    swing: float
    p_qualify_home_win: float
    p_qualify_draw: float
    p_qualify_away_win: float
    is_own_match: bool = False
    computed_at: datetime | None = None
```

**Step 5: Commit**

```bash
git add projects/monolith/chart/migrations/20260620120000_worldcup_schema.sql \
        projects/monolith/chart/migrations/20260620120100_worldcup_public_reader_grant.sql \
        projects/monolith/chart/migrations/atlas.sum \
        projects/monolith/worldcup/__init__.py \
        projects/monolith/worldcup/models.py
git commit -m "feat(worldcup): add schema, public_reader grant, and SQLModel tables"
```

---

### Task 2: Elo ratings snapshot + loader

**Files:**

- Create: `projects/monolith/worldcup/ratings/elo_2026.json`
- Create: `projects/monolith/worldcup/ratings.py`
- Test: `projects/monolith/worldcup/ratings_test.py`

**Step 1: Build the Elo snapshot.** Fetch World Football Elo ratings (eloratings.net) as of June 2026 for all 48 WC2026 teams. Cross-reference team `fifa_code`s from the API (`curl -s 'https://worldcup26.ir/get/teams'`). Write a JSON object keyed by `fifa_code`:

```json
{
  "_source": "eloratings.net snapshot, tournament start June 2026",
  "_note": "National-team Elo is near-static over a 3-week tournament; this snapshot is intentionally frozen for reproducibility.",
  "ratings": {
    "BRA": 2030,
    "MAR": 1850,
    "SCO": 1720,
    "HAI": 1480
  }
}
```

Fill ALL 48 codes. A missing code must be a hard error at sim time (Task 4), never a silent default, so completeness matters. Keep this file under a few KB (it is committed code, not migration data, so the 256 KiB ConfigMap cap does not apply).

**Step 2: Write the failing test** `ratings_test.py`:

```python
from worldcup import ratings

def test_loads_all_48_teams():
    table = ratings.load_elo()
    assert len(table) == 48
    assert table["SCO"] > 0

def test_get_raises_on_unknown_code():
    table = ratings.load_elo()
    import pytest
    with pytest.raises(KeyError):
        ratings.elo_for(table, "ZZZ")
```

**Step 3: Implement `ratings.py`:**

```python
"""Load the committed Elo snapshot for the 48 WC2026 teams."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).parent / "ratings" / "elo_2026.json"


@lru_cache(maxsize=1)
def load_elo() -> dict[str, float]:
    data = json.loads(_PATH.read_text())["ratings"]
    return {code: float(v) for code, v in data.items()}


def elo_for(table: dict[str, float], fifa_code: str) -> float:
    return table[fifa_code]  # KeyError if a team is missing from the snapshot
```

**Step 4 (expected on CI):** both tests pass; the 48-team assertion guards against an incomplete snapshot.

**Step 5: Commit**

```bash
git add projects/monolith/worldcup/ratings/elo_2026.json \
        projects/monolith/worldcup/ratings.py \
        projects/monolith/worldcup/ratings_test.py
git commit -m "feat(worldcup): add frozen Elo snapshot and loader"
```

---

### Task 3: Match-outcome probability model

The core model. Given two Elo ratings, sample a scoreline. We need full scorelines (not just W/D/L) because the third-place tiebreakers use goal difference and goals scored.

**Model (independent Poisson, neutral venue):**

- `we = 1 / (1 + 10 ** (-(elo_home - elo_away) / 400))` is the home team's Elo win-expectancy in `[0,1]`.
- Split an average match goal total `AVG_TOTAL = 2.6` by win-expectancy: `lambda_home = AVG_TOTAL * we`, `lambda_away = AVG_TOTAL * (1 - we)`.
- Sample `home ~ Poisson(lambda_home)`, `away ~ Poisson(lambda_away)` with a seedable RNG.

This yields a ~24% draw rate for evenly matched teams (matches historical) and a monotonically increasing win rate for the stronger side, with no external odds source.

**Files:**

- Create: `projects/monolith/worldcup/outcome.py`
- Test: `projects/monolith/worldcup/outcome_test.py`

**Step 1: Write the failing tests** `outcome_test.py`:

```python
import random
from worldcup.outcome import win_expectancy, sample_scoreline, outcome_probabilities


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
```

**Step 2: Implement `outcome.py`:**

```python
"""Elo -> scoreline sampler. Independent-Poisson model, neutral venue."""
from __future__ import annotations

import random

AVG_TOTAL = 2.6  # average total goals per match (WC-era baseline)


def win_expectancy(elo_home: float, elo_away: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(elo_home - elo_away) / 400.0))


def _poisson(lam: float, rng: random.Random) -> int:
    # Knuth's algorithm; lam is small (<3) so this is cheap.
    target = pow(2.718281828459045, -lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= target:
            return k - 1


def sample_scoreline(elo_home: float, elo_away: float, rng: random.Random) -> tuple[int, int]:
    we = win_expectancy(elo_home, elo_away)
    return _poisson(AVG_TOTAL * we, rng), _poisson(AVG_TOTAL * (1 - we), rng)


def outcome_probabilities(elo_home, elo_away, rng, n=20000) -> dict[str, float]:
    hw = d = aw = 0
    for _ in range(n):
        h, a = sample_scoreline(elo_home, elo_away, rng)
        if h > a:
            hw += 1
        elif h == a:
            d += 1
        else:
            aw += 1
    return {"home_win": hw / n, "draw": d / n, "away_win": aw / n}
```

**Step 3: Commit**

```bash
git add projects/monolith/worldcup/outcome.py projects/monolith/worldcup/outcome_test.py
git commit -m "feat(worldcup): add Elo-based Poisson scoreline model"
```

---

### Task 4: Monte Carlo qualification simulation

Simulates ALL remaining group games across all 12 groups, applies the WC2026 advancement rules (top 2 per group + 8 best thirds), and reports per-team probabilities plus per-match swings for the focus team.

**Inputs (plain dataclasses/dicts, no DB):** current standings (per team: group, pts, gf, ga, plus fifa_code) and remaining fixtures (group, home_code, away_code, match_id). The DB-to-input adapter lives in Task 6, so the sim stays pure and unit-testable.

**Advancement rules to encode (from FIFA WC2026):**

- Within a group: rank by pts, then gd, then gf. Remaining ties (cards, FIFA rank) are unknowable from our data, so break them with `rng` (a coin flip), which is the honest representation of "too close to call."
- Top 2 of every group qualify directly.
- Pool the 3rd-placed team from each of the 12 groups; rank them by pts, then gd, then gf (then `rng`); the top 8 qualify.

**Files:**

- Create: `projects/monolith/worldcup/sim.py`
- Test: `projects/monolith/worldcup/sim_test.py`

**Step 1: Write failing tests** `sim_test.py` (use tiny synthetic tournaments with known answers):

```python
import random
from worldcup.sim import TeamState, Fixture, simulate


def _two_team_group(group, a, b, elo):
    # helper to build minimal states
    ...

def test_clinched_team_is_certain():
    # A team that has already finished top-2 with all its games played and an
    # insurmountable lead must come out at prob_qualify == 1.0.
    states, fixtures = _scenario_already_top2_no_games_left()
    res = simulate(states, fixtures, elo={...}, focus="SCO", n=2000, seed=1)
    assert res.per_team["SCO"].prob_qualify == 1.0
    assert res.per_team["SCO"].status == "qualified"

def test_eliminated_team_is_zero():
    states, fixtures = _scenario_bottom_no_path()
    res = simulate(states, fixtures, elo={...}, focus="HAI", n=2000, seed=1)
    assert res.per_team["HAI"].prob_qualify == 0.0
    assert res.per_team["HAI"].status == "eliminated"

def test_probabilities_in_unit_interval_and_route_split():
    states, fixtures = _scenario_scotland_like()
    res = simulate(states, fixtures, elo={...}, focus="SCO", n=5000, seed=3)
    p = res.per_team["SCO"]
    assert 0.0 <= p.prob_qualify <= 1.0
    # qualify is top2 OR third; the two routes are mutually exclusive so they sum
    assert abs(p.prob_qualify - (p.prob_top2 + p.prob_third)) < 1e-9

def test_swing_identifies_own_match_as_high_impact():
    states, fixtures = _scenario_scotland_like()
    res = simulate(states, fixtures, elo={...}, focus="SCO", n=5000, seed=5)
    own = [s for s in res.swings if s.is_own_match]
    assert own and own[0].swing > 0
    # Scotland's own remaining match should be among the highest swings
    assert res.swings[0].swing >= own[0].swing
```

(Implementer: write the `_scenario_*` helpers as small fixed dicts; keep them analytically obvious so the assertions are deterministic given the seed.)

**Step 2: Implement `sim.py`.** Key structure:

```python
"""Monte Carlo simulation of WC2026 group-stage qualification."""
from __future__ import annotations

import math
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
    swings: list[Swing]            # sorted by swing desc
    n: int


def simulate(states, fixtures, elo, focus, n=20000, seed=None) -> SimResult:
    rng = random.Random(seed)
    by_code = {s.fifa_code: s for s in states}
    qualify_count = {s.fifa_code: 0 for s in states}
    top2_count = {s.fifa_code: 0 for s in states}
    third_count = {s.fifa_code: 0 for s in states}

    # For swing: for each remaining fixture, tally focus-qualified counts split
    # by that fixture's sampled outcome (home_win / draw / away_win).
    swing_tally = {
        f.match_id: {"home_win": [0, 0], "draw": [0, 0], "away_win": [0, 0]}
        for f in fixtures
    }  # each value = [focus_qualified, total]

    for _ in range(n):
        # 1. fresh accumulators from current standings
        acc = {c: [s.pts, s.gf, s.ga] for c, s in by_code.items()}
        outcomes = {}
        for f in fixtures:
            h, a = sample_scoreline(elo[f.home_code], elo[f.away_code], rng)
            acc[f.home_code][1] += h; acc[f.home_code][2] += a
            acc[f.away_code][1] += a; acc[f.away_code][2] += h
            if h > a:
                acc[f.home_code][0] += 3; outcomes[f.match_id] = "home_win"
            elif h == a:
                acc[f.home_code][0] += 1; acc[f.away_code][0] += 1
                outcomes[f.match_id] = "draw"
            else:
                acc[f.away_code][0] += 3; outcomes[f.match_id] = "away_win"

        # 2. rank within each group; collect top2 + the 3rd-placed pool
        thirds = []
        group_codes = {}
        for s in states:
            group_codes.setdefault(s.group, []).append(s.fifa_code)
        qualified = set()
        for g, codes in group_codes.items():
            ranked = sorted(
                codes,
                key=lambda c: (acc[c][0], acc[c][1] - acc[c][2], acc[c][1], rng.random()),
                reverse=True,
            )
            qualified.update(ranked[:2])
            for c in ranked[:2]:
                top2_count[c] += 1
            if len(ranked) >= 3:
                thirds.append(ranked[2])

        # 3. rank the 12 thirds; top 8 qualify
        thirds_ranked = sorted(
            thirds,
            key=lambda c: (acc[c][0], acc[c][1] - acc[c][2], acc[c][1], rng.random()),
            reverse=True,
        )
        third_qualifiers = set(thirds_ranked[:8])
        qualified |= third_qualifiers
        for c in third_qualifiers:
            third_count[c] += 1

        # 4. tally
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
            status="qualified" if pq == 1.0 else "eliminated" if pq == 0.0 else "contention",
        )

    swings = []
    for f in fixtures:
        conds = {}
        for oc in ("home_win", "draw", "away_win"):
            q, t = swing_tally[f.match_id][oc]
            conds[oc] = (q / t) if t else per_team[focus].prob_qualify
        swing = max(conds.values()) - min(conds.values())
        swings.append(Swing(
            match_id=f.match_id, group=f.group, home_code=f.home_code,
            away_code=f.away_code, swing=swing,
            p_qualify_home_win=conds["home_win"], p_qualify_draw=conds["draw"],
            p_qualify_away_win=conds["away_win"], is_own_match=f.is_own,
        ))
    swings.sort(key=lambda s: s.swing, reverse=True)
    return SimResult(per_team=per_team, swings=swings, n=n)
```

Notes for the implementer:

- The conditional-probability swing reuses the single Monte Carlo run, so there is no extra cost and it naturally surfaces Scotland vs Brazil AND the relevant other-group third-place deciders.
- `prob_top2 + prob_third == prob_qualify` because top-2 and best-third are mutually exclusive per simulation; the test asserts this identity.
- Keep `n` configurable; default `20000` is plenty (Monte Carlo error on a probability is ~`0.5/sqrt(n)` ≈ 0.35%).

**Step 3: Commit**

```bash
git add projects/monolith/worldcup/sim.py projects/monolith/worldcup/sim_test.py
git commit -m "feat(worldcup): add Monte Carlo qualification + swing simulation"
```

---

### Task 5: worldcup26.ir API client

Pure fetch/parse, no DB. Mirrors `dr_jobs/scraper.py` (never raises on a bad payload; returns parsed lists + a stats dict).

**Files:**

- Create: `projects/monolith/worldcup/client.py`
- Test: `projects/monolith/worldcup/client_test.py`

**Step 1: Write failing tests** using captured fixture JSON (paste a trimmed real sample from `curl -s https://worldcup26.ir/get/groups | head` and `/get/games`). Assert:

- `parse_standings({"groups": [...]})` returns one record per team with int-coerced `pts/gf/ga/gd` (the API returns these as strings).
- `parse_fixtures({"games": [...]})` filters to `type == "group"`, coerces `finished` (`"TRUE"`/`"FALSE"` -> bool), parses `local_date` (`MM/DD/YYYY HH:MM`) to an aware UTC datetime, and leaves scores `None` when not finished.

```python
from worldcup.client import parse_standings, parse_fixtures

def test_parse_standings_coerces_ints():
    rows = parse_standings({"groups": [
        {"name": "C", "teams": [
            {"team_id": "12", "mp": "2", "w": "1", "d": "0", "l": "1",
             "pts": "3", "gf": "1", "ga": "1", "gd": "0"}]}]})
    r = rows[0]
    assert r["group_name"] == "C" and r["pts"] == 3 and isinstance(r["pts"], int)

def test_parse_fixtures_group_only_and_bool_finished():
    games = {"games": [
        {"id": "1", "type": "group", "group": "C", "matchday": "3",
         "home_team_id": "12", "home_team_name_en": "Scotland",
         "away_team_id": "9", "away_team_name_en": "Brazil",
         "home_score": "0", "away_score": "0", "finished": "FALSE",
         "local_date": "06/24/2026 18:00"},
        {"id": "200", "type": "knockout", "group": "", "matchday": "4"}]}
    fx = parse_fixtures(games)
    assert len(fx) == 1
    assert fx[0]["finished"] is False
    assert fx[0]["home_score"] is None  # not finished -> no score
```

**Step 2: Implement `client.py`** with `fetch_all(client) -> tuple[list, list, dict]` (async httpx GETs to `/get/groups` and `/get/games`, base URL from `WORLDCUP_API_BASE` env defaulting to `https://worldcup26.ir`), plus the two pure parse functions. Map team flag/name into standings from the groups payload; if the groups payload lacks names, additionally GET `/get/teams` and join on `team_id`. Coerce all numerics defensively (`int(x or 0)`), parse `local_date` as `America/New_York`? No: the API's `local_date` is venue-local and ambiguous; store it as naive-UTC-tagged best effort and surface "kickoff" only for display. Simpler and robust: keep `kickoff` as the parsed datetime in UTC assuming the string is Eastern, OR just store the raw string in a display field. **Decision: store `kickoff` parsed as US Eastern -> UTC** (most venues are Eastern; minor display skew is acceptable for a hobby page) and document it.

**Step 3: Commit**

```bash
git add projects/monolith/worldcup/client.py projects/monolith/worldcup/client_test.py
git commit -m "feat(worldcup): add worldcup26.ir API client and parsers"
```

---

### Task 6: Scheduled job (poll -> upsert -> simulate -> store)

Wires client + sim + DB together. Mirrors `dr_jobs/jobs.py`: async network phase, DB work in `asyncio.to_thread`.

**Files:**

- Create: `projects/monolith/worldcup/jobs.py`
- Modify: `projects/monolith/worldcup/__init__.py` (add `on_startup_jobs`)
- Test: `projects/monolith/worldcup/jobs_test.py` (test the pure DB-to-sim adapter against a SQLite session; mock the network)

**Step 1: Write the failing test** for the adapter `_build_sim_inputs(session) -> (states, fixtures)` and `_persist_sim(session, result)`:

- Seed SQLite `worldcup.standings` + `worldcup.fixtures` (one unfinished Scotland game), call `_build_sim_inputs`, assert the focus fixture has `is_own=True` and only unfinished fixtures are included.
- Call `_persist_sim` with a hand-built `SimResult`, assert a `qualification` row for `SCO` and `swing_matches` rows exist with the right `swing` ordering.

**Step 2: Implement `jobs.py`:**

```python
FOCUS_CODE = "SCO"

async def refresh_handler(session) -> datetime | None:
    import httpx
    base = os.environ.get("WORLDCUP_API_BASE", "https://worldcup26.ir")
    async with httpx.AsyncClient(base_url=base, timeout=20) as client:
        standings, fixtures, stats = await client_mod.fetch_all(client)
    await asyncio.to_thread(_upsert, standings, fixtures)
    await asyncio.to_thread(_simulate_and_store)
    return None  # let scheduler compute next run from interval

def _upsert(standings, fixtures):
    with get_session() as s:
        # upsert standings + fixtures (delete-absent or merge by PK)
        ...

def _simulate_and_store():
    with get_session() as s:
        states, fxs = _build_sim_inputs(s)
        elo = ratings.load_elo()
        result = simulate(states, fxs, elo, focus=FOCUS_CODE,
                          n=int(os.environ.get("WORLDCUP_SIM_N", "20000")), seed=None)
        _persist_sim(s, result)
```

- `_build_sim_inputs` reads `worldcup.standings` into `TeamState`s and unfinished `worldcup.fixtures` into `Fixture`s, marking `is_own = FOCUS_CODE in (home_code, away_code)`.
- `_persist_sim` upserts `worldcup.qualification` for all teams and replaces `worldcup.swing_matches` for the focus team (delete-all-then-insert is fine; it is a small table).
- Guard the Elo lookup: if a fixture references a code absent from the snapshot, log and skip the sim (do NOT default), so an incomplete snapshot fails loudly.

**Step 3: Implement `on_startup_jobs` in `__init__.py`:**

```python
def on_startup_jobs(session) -> None:
    from scheduler.api import register_job
    from worldcup.jobs import refresh_handler
    register_job(session, name="worldcup.refresh",
                 interval_secs=1800, handler=refresh_handler, ttl_secs=600)
```

(30-minute cadence: standings change only when a match finishes, a few times a day, so 30 min is responsive without hammering the upstream.)

**Step 4: Commit**

```bash
git add projects/monolith/worldcup/jobs.py projects/monolith/worldcup/jobs_test.py \
        projects/monolith/worldcup/__init__.py
git commit -m "feat(worldcup): add scheduled poll+simulate refresh job"
```

---

### Task 7: Read router (SSR-only public API)

**Files:**

- Create: `projects/monolith/worldcup/router.py`
- Modify: `projects/monolith/worldcup/__init__.py` (add `register` + `register_public` aliasing it, like `dr_jobs`)
- Test: `projects/monolith/worldcup/router_test.py`

**Step 1: Write the failing test** (FastAPI `TestClient` + SQLite, seed the four tables):

- `GET /api/wc2026/summary` returns `{ focus, group_table, qualification, swing_matches, updated_at }` where `qualification` is Scotland's row with `prob_qualify/prob_top2/prob_third/status`, `group_table` is Group C sorted by pts/gd/gf, and `swing_matches` is sorted by `swing` desc.
- Response sets `Cache-Control` (e.g. `public, max-age=300`) and a data-derived `ETag`; a matching `If-None-Match` yields `304`.

**Step 2: Implement `router.py`** with a single `GET /api/wc2026/summary` (one endpoint keeps SSR simple). Read Scotland's group from `standings` (where `fifa_code='SCO'`), then the full group table for that group, the `qualification` row for `SCO`, and all `swing_matches` ordered by `swing` desc (limit ~8). Use the `_as_utc` helper for timestamps. Mark the router SSR-only in the docstring: **never add to `httproute-public.yaml`** (this is what makes the page unlisted: reachable via the SSR page, but the JSON endpoint is not a public gateway route).

```python
def register(app):
    app.include_router(_router)

register_public = register  # read-only; safe in the public binary
```

**Step 3: Commit**

```bash
git add projects/monolith/worldcup/router.py projects/monolith/worldcup/router_test.py \
        projects/monolith/worldcup/__init__.py
git commit -m "feat(worldcup): add SSR-only wc2026 summary read endpoint"
```

---

### Task 8: App wiring (private + public binaries)

**Files:**

- Modify: `projects/monolith/app/main.py` (register router + on_startup_jobs in lifespan)
- Modify: `projects/monolith/app/main_public.py` (register_public)

**Step 1:** In `app/main.py`, alongside the existing `dr_jobs` hooks:

- in the lifespan startup block (near line 74): `worldcup.on_startup_jobs(session)`
- in router registration (near line 215): `worldcup.register(app)`
- add `import worldcup` at the top with the other domain imports.

**Step 2:** In `app/main_public.py` (near line 39-46, with the other `register_public` calls): `worldcup.register_public(app)` and `import worldcup`.

**Step 3:** Update `projects/monolith/app/main_public_imports_test.py` expectations only if it enumerates allowed modules; the import-boundary test (`import_boundaries_test.py`) must still pass, so ensure the public binary's import of `worldcup` does NOT transitively import `jobs.py`/`client.py`/`sim.py` (network/sim). Achieve this by importing those lazily inside functions (the `__init__.py` `register_public` must only need `router` + `models`, not `jobs`). Verify no module-level `from worldcup.jobs import ...` in `__init__.py`.

**Step 4: Commit**

```bash
git add projects/monolith/app/main.py projects/monolith/app/main_public.py
git commit -m "feat(worldcup): wire worldcup into private and public app entrypoints"
```

---

### Task 9: Bazel BUILD wiring

The `worldcup` dir is gazelle-excluded, so hand-edit globs. Mirror how `trips` splits public/private.

**Files:**

- Modify: `projects/monolith/BUILD`

**Step 1:** Add `# gazelle:exclude worldcup` near the other domain excludes (top of file, ~lines 1-17).

**Step 2:** Add `"worldcup/**/*.py"` to:

- the `:main` `py_venv_binary` srcs glob (~line 63)
- the `:monolith_backend` `py_library` srcs glob (~line 156)
- the `:monolith_public_backend` `py_library` srcs glob (~line 312)

**Step 3:** Add the private-only files to `_PUBLIC_PRUNE_EXCLUDE` (~line 260) so they are pruned from the public binary (the public tier must not carry the poller/sim/client):

```
"worldcup/jobs.py",
"worldcup/client.py",
"worldcup/sim.py",
```

Keep `worldcup/models.py`, `worldcup/router.py`, `worldcup/ratings.py`, `worldcup/outcome.py`, `worldcup/__init__.py` in the public binary (router needs models; ratings/outcome are tiny and pure, harmless, and `outcome` is imported by `sim` only). If the import-boundary test flags `outcome`/`ratings` as unreachable-and-unwanted in public, also add them to the prune list; the safe default is to prune anything only the sim path needs: `outcome.py`, `ratings.py`, and the `ratings/` data are sim-only, so prune them too and keep only `models.py` + `router.py` + `__init__.py` public.

**Step 4:** Ensure the new `*_test.py` files are picked up. If the BUILD declares tests via a glob they are automatic; if domain tests are hand-listed (like the grant-boundary tests at lines 20-26), add `py_test` targets for `outcome_test`, `sim_test`, `client_test`, `jobs_test`, `router_test`, `ratings_test`. Check how `dr_jobs/*_test.py` are registered and copy that exact mechanism.

**Step 5: Commit**

```bash
git add projects/monolith/BUILD
git commit -m "build(worldcup): register module in private/public binaries and tests"
```

---

### Task 10: Frontend SSR page (unlisted)

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/app/wc2026/+page.server.js`
- Create: `projects/monolith/frontend/src/routes/public/app/wc2026/+page.svelte`

**Step 1: `+page.server.js`** (copy `dr-jobs/+page.server.js` shape):

```javascript
import { error } from "@sveltejs/kit";
const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/wc2026/summary`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw error(503, "wc2026 data unavailable");
  setHeaders({ "cache-control": "public, max-age=300" });
  return await res.json();
}
```

**Step 2: `+page.svelte`** renders:

- Headline: "Scotland's chance of reaching the Round of 32: **{prob_qualify}%**" with `status` overrides ("Qualified" / "Eliminated").
- Route breakdown: "As group runner-up or better: {prob_top2}% / As one of the 8 best third-placed teams: {prob_third}%" with a one-line note that the top-2 route is the long shot (the Elo model already reflects this; no manual caveat number needed).
- Group C table (the `group_table` array).
- "Matches that could change it": the `swing_matches` list, each row showing `{home_code} v {away_code}`, kickoff, and the three conditional probabilities (home win / draw / away win) with the swing magnitude; Scotland's own match flagged.
- Footer: data source (`worldcup26.ir`), model ("Elo-weighted Monte Carlo, {n_sims} simulations"), the tiebreaker caveat (cards / FIFA ranking not modelled, shown as coin-flips), and `Last updated {updated_at}`.

Keep styling consistent with the existing public pages (reuse the same tokens/components as `dr-jobs`). Do NOT add a nav link anywhere: the page is unlisted, reachable only by URL.

**Step 3: Visual-regression fixture.** Add `wc2026` to `projects/monolith/frontend/visual/targets.json` and provide a mock fixture for `/api/wc2026/summary` in the visual mock server (`frontend/visual/mock-server.mjs` or equivalent), so the public-page visual regression covers it. Follow the pattern documented for `dr-jobs`.

**Step 4: Commit**

```bash
git add projects/monolith/frontend/src/routes/public/app/wc2026/ \
        projects/monolith/frontend/visual/
git commit -m "feat(worldcup): add unlisted Scotland wc2026 odds public page"
```

---

### Task 11: Chart version bumps + push + CI

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (bump version) + `projects/monolith/deploy/application.yaml` (`targetRevision`)
- Modify: `projects/monolith-public/chart/Chart.yaml` + `projects/monolith-public/deploy/application.yaml` if the public page ships via the separate `monolith-public` chart (verify which chart serves the public frontend/backend; the recent commit history bumps both `monolith` and `monolith-public` charts).
- Possibly add `WORLDCUP_API_BASE` / `WORLDCUP_SIM_N` env to `projects/monolith/deploy/values.yaml` (optional; defaults are fine).

**Step 1:** Run `format` from the worktree (updates BUILD files / home-cluster kustomization, applies gofumpt/ruff/prettier). Resolve any format drift.

**Step 2:** Bump the chart version(s). The `chart-version-bot` normally keeps `Chart.yaml` and `application.yaml` `targetRevision` in sync, but bump both manually to be safe (a mismatch means ArgoCD never pulls the new chart). Confirm whether the public page requires the `monolith-public` chart bump by checking which chart's image/templates include the `/app/wc2026` route and the migrations.

**Step 3:** Push the branch and open the PR:

```bash
git push -u origin feat/wc2026-scotland-odds
gh pr create --fill --title "feat(worldcup): Scotland WC2026 qualification odds page"
```

**Step 4: Watch CI** (this is where all tests finally run):

```bash
gh pr checks <number> --watch
```

Iterate on failures by reading logs via `mcp__buildbuddy__get_invocation` (use the `commitSha` selector) -> `get_target` -> `get_log`. Likely first-pass issues to expect and fix:

- `atlas.sum` hash mismatch (Task 1 Step 3) -> apply the hash CI reports.
- Import-boundary test failures -> tighten the public prune list / make `jobs`/`client`/`sim` imports lazy.
- Semgrep `no-hardcoded-k8s-service-url` is N/A (we use `WORLDCUP_API_BASE`); `no-hardcoded-image-digest` N/A. If Semgrep Pro false-positives on anything, exclude in the BUILD entry (do not add `# nosemgrep` for `main_semgrep_test`).
- Visual-regression baseline: generate/commit the new baseline screenshot per the documented `dr-jobs` flow.

**Step 5:** Once green, the end-of-PR comprehensive code review (per `.claude/CLAUDE.md` cadence: one review per merged PR), then `gh pr merge --rebase`.

**Step 6:** After merge, verify live: poll the ArgoCD app sync, then `curl -s https://jomcgi.dev/app/wc2026` returns the page and `https://jomcgi.dev/api/...` is NOT exposed (the endpoint is SSR-only). Confirm the scheduled `worldcup.refresh` job has run (the `qualification` table has a fresh `computed_at`).

---

## Verification checklist (end state)

- [ ] `jomcgi.dev/app/wc2026` renders Scotland's qualification % with a route breakdown.
- [ ] "Matches that could change it" lists Scotland vs Brazil plus the highest-impact other-group third-place deciders, ranked by real swing.
- [ ] Numbers refresh after each finished match (within the 30-min job cadence).
- [ ] Public tier reads only (no egress, no compute); private job does all fetching + simulation.
- [ ] Tiebreaker caveat and data source/last-updated shown in the footer.
- [ ] CI green; one end-of-PR code review done; merged via rebase.

```

```
