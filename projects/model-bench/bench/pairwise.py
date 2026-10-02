"""Pairwise LLM judge over passing cells, fitted to Bradley-Terry ratings (#6700).

Passing the verifier is the floor and the deterministic norms (bench/norms.py) rank
the cheap signals. This ranks what neither can see: given two passing diffs for the
same task, which one would a careful reviewer of this repo rather merge?

- Blind: the judge sees two diffs labelled A and B, never the model ids.
- Position-debiased: every pair is judged twice with A and B swapped, and only a
  verdict that agrees across both orders counts. A flip counts as a tie.
- No self-judging: a pair containing the judge model's own output goes to the
  fallback judge, and is skipped if the fallback is in the pair too.
- Cached: verdicts are keyed by both diffs, the rubric version and the judge model,
  so a rerun only pays for new pairs.

Ratings come from a Bradley-Terry fit (minorisation-maximisation, ties as half a
win each way) with bootstrap confidence intervals over the judged pairs.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

RUBRIC_VERSION = "1"
RUBRIC = """\
Judge which change a careful reviewer of this repository would rather merge. Both
changes already pass the task's hidden tests, so do not re-check correctness against
the task; judge the quality of the change itself:

1. Scope: it changes only what the task needs. No unrelated edits, no speculative
   features, no reformatting of untouched code.
2. Minimal and clear: the smallest diff that solves the problem properly, readable
   without explanation. Prefer fixing the root cause over special-casing.
3. Matches the surrounding code: same naming, idiom, error handling and comment
   density as the files it touches.
4. Tests: behaviour changes come with focused tests that would fail without the fix.
5. Repo invariants: no em-dashes, no hardcoded secrets, no chart version bumps, no
   debug output or TODOs left behind.
6. Safety: no new security or data-loss risk (authz, input handling, destructive
   operations)."""

DEFAULT_JUDGE = "claude-opus-5-5"
FALLBACK_JUDGE = "claude-sonnet-5-5"
DIFF_CAP = 60_000


@dataclass(frozen=True)
class Candidate:
    model_id: str
    task_id: str
    diff: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.diff.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Verdict:
    task_id: str
    a: str  # model id
    b: str
    winner: str | None  # model id, or None for a tie (including order disagreement)
    judge: str


def _cli_name(model_id: str) -> str:
    return model_id.removeprefix("anthropic/").replace(".", "-")


def pick_judge(
    a: str, b: str, judge: str = DEFAULT_JUDGE, fallback: str = FALLBACK_JUDGE
) -> str | None:
    """The judge for a pair: never a model judging its own output."""
    names = {_cli_name(a), _cli_name(b)}
    if judge not in names:
        return judge
    if fallback not in names:
        return fallback
    return None


def build_prompt(task_prompt: str, first: str, second: str, gold: str | None) -> str:
    gold_block = (
        "A reference fix from the real repository history (for orientation only; "
        f"the candidates need not match it):\n```diff\n{gold[:DIFF_CAP]}\n```\n\n"
        if gold
        else ""
    )
    return (
        f"{RUBRIC}\n\n"
        f"The task the changes were written for:\n<task>\n{task_prompt}\n</task>\n\n"
        f"{gold_block}"
        f"Change A:\n```diff\n{first[:DIFF_CAP]}\n```\n\n"
        f"Change B:\n```diff\n{second[:DIFF_CAP]}\n```\n\n"
        "Weigh the rubric, then end with a final line exactly of the form "
        "'WINNER: A', 'WINNER: B' or 'WINNER: TIE'."
    )


def parse_winner(text: str) -> str:
    """'A', 'B' or 'TIE' from the last WINNER line; TIE when absent."""
    for line in reversed(text.strip().splitlines()):
        up = line.strip().upper()
        if up.startswith("WINNER:"):
            choice = up.split(":", 1)[1].strip()
            if choice.startswith("A"):
                return "A"
            if choice.startswith("B"):
                return "B"
            return "TIE"
    return "TIE"


class VerdictCache:
    """On-disk verdict cache: one JSON file per (pair, rubric, judge)."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, x: Candidate, y: Candidate, judge: str) -> Path:
        lo, hi = sorted([x.digest, y.digest])
        key = hashlib.sha256(
            f"{x.task_id}|{lo}|{hi}|{RUBRIC_VERSION}|{judge}".encode()
        ).hexdigest()[:24]
        return self.root / f"{key}.json"

    def get(self, x: Candidate, y: Candidate, judge: str) -> str | None:
        """Cached winner digest ('' for a tie), or None when not cached."""
        p = self._path(x, y, judge)
        if not p.exists():
            return None
        return json.loads(p.read_text())["winner_digest"]

    def put(self, x: Candidate, y: Candidate, judge: str, winner_digest: str) -> None:
        p = self._path(x, y, judge)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"winner_digest": winner_digest, "judge": judge}))


def judge_pair(
    x: Candidate,
    y: Candidate,
    *,
    task_prompt: str,
    gold: str | None,
    caller: Callable[[str, str], str],
    cache: VerdictCache | None,
    judge: str,
    rng: random.Random,
) -> Verdict:
    """Judge x vs y in both orders; keep the verdict only if the orders agree."""
    cached = cache.get(x, y, judge) if cache else None
    if cached is None:
        first, second = (x, y) if rng.random() < 0.5 else (y, x)
        one = parse_winner(
            caller(build_prompt(task_prompt, first.diff, second.diff, gold), judge)
        )
        two = parse_winner(
            caller(build_prompt(task_prompt, second.diff, first.diff, gold), judge)
        )
        pick = {"A": first, "B": second}
        flip = {"A": second, "B": first}
        w1 = pick.get(one)
        w2 = flip.get(two)
        agreed = w1 if (w1 is not None and w1 is w2) else None
        cached = agreed.digest if agreed else ""
        if cache:
            cache.put(x, y, judge, cached)
    winner = x.model_id if cached == x.digest else y.model_id if cached else None
    return Verdict(x.task_id, x.model_id, y.model_id, winner, judge)


def judge_all(
    candidates: list[Candidate],
    *,
    prompts: dict[str, str],
    golds: dict[str, str],
    caller: Callable[[str, str], str],
    cache: VerdictCache | None,
    judge: str = DEFAULT_JUDGE,
    fallback: str = FALLBACK_JUDGE,
    max_pairs_per_task: int | None = None,
    seed: int = 0,
) -> tuple[list[Verdict], int]:
    """Judge every pair of passing candidates within each task.

    Returns the verdicts and the number of pairs skipped because no judge was
    independent of both candidates.
    """
    rng = random.Random(seed)
    by_task: dict[str, list[Candidate]] = {}
    for c in candidates:
        if c.diff.strip():
            by_task.setdefault(c.task_id, []).append(c)
    verdicts: list[Verdict] = []
    skipped = 0
    for task_id in sorted(by_task):
        pairs = list(itertools.combinations(by_task[task_id], 2))
        if max_pairs_per_task is not None and len(pairs) > max_pairs_per_task:
            pairs = rng.sample(pairs, max_pairs_per_task)
        for x, y in pairs:
            who = pick_judge(x.model_id, y.model_id, judge, fallback)
            if who is None:
                skipped += 1
                continue
            verdicts.append(
                judge_pair(
                    x,
                    y,
                    task_prompt=prompts.get(task_id, ""),
                    gold=golds.get(task_id),
                    caller=caller,
                    cache=cache,
                    judge=who,
                    rng=rng,
                )
            )
    return verdicts, skipped


def bradley_terry(
    verdicts: list[Verdict], iters: int = 200, prior: float = 0.5
) -> dict[str, float]:
    """Bradley-Terry strengths via minorisation-maximisation.

    Ties count as half a win to each side. A small prior win/loss against a virtual
    average opponent keeps models with no wins (or no losses) finite. Ratings are
    returned on an Elo-like scale centred on 0 (400 * log10 of strength).
    """
    models = sorted({v.a for v in verdicts} | {v.b for v in verdicts})
    if not models:
        return {}
    wins = {m: prior for m in models}
    games: dict[tuple[str, str], float] = {}
    for v in verdicts:
        key = tuple(sorted((v.a, v.b)))
        games[key] = games.get(key, 0.0) + 1.0
        if v.winner is None:
            wins[v.a] += 0.5
            wins[v.b] += 0.5
        else:
            wins[v.winner] += 1.0
    p = {m: 1.0 for m in models}
    for _ in range(iters):
        new = {}
        for m in models:
            denom = 2 * prior / (p[m] + 1.0)  # virtual opponent of strength 1
            for (i, j), n in games.items():
                if m in (i, j):
                    other = j if m == i else i
                    denom += n / (p[m] + p[other])
            new[m] = wins[m] / denom
        # Normalise to a geometric mean of 1 so the scale stays put.
        g = math.exp(sum(math.log(x) for x in new.values()) / len(new))
        p = {m: x / g for m, x in new.items()}
    return {m: 400 * math.log10(x) for m, x in p.items()}


def ratings_with_ci(
    verdicts: list[Verdict], boot: int = 200, seed: int = 0
) -> dict[str, dict]:
    """Bradley-Terry ratings plus a 90% bootstrap interval per model."""
    point = bradley_terry(verdicts)
    if not verdicts:
        return {}
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {m: [] for m in point}
    for _ in range(boot):
        resample = [rng.choice(verdicts) for _ in verdicts]
        fit = bradley_terry(resample, iters=100)
        for m in point:
            if m in fit:
                samples[m].append(fit[m])
    out = {}
    for m, r in point.items():
        s = sorted(samples[m])
        lo = s[int(0.05 * (len(s) - 1))] if s else r
        hi = s[int(0.95 * (len(s) - 1))] if s else r
        games = sum(1 for v in verdicts if m in (v.a, v.b))
        out[m] = {
            "judge_rating": round(r, 1),
            "judge_ci": [round(lo, 1), round(hi, 1)],
            "judge_games": games,
        }
    return out
