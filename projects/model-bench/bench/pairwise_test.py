import random

import pytest

from bench import pairwise
from bench.pairwise import Candidate, Verdict


def _c(model, diff, task="t"):
    return Candidate(model, task, diff)


def _caller_preferring(marker):
    """A fake judge that picks whichever change contains marker, in either slot."""
    calls = []

    def caller(prompt, judge):
        calls.append(judge)
        a = prompt.split("Change A:", 1)[1].split("Change B:", 1)[0]
        return "reasoning...\nWINNER: A" if marker in a else "WINNER: B"

    caller.calls = calls
    return caller


def test_parse_winner_reads_last_line_and_defaults_to_tie():
    assert pairwise.parse_winner("blah\nWINNER: A") == "A"
    assert pairwise.parse_winner("WINNER: B\nmore\nWINNER: tie") == "TIE"
    assert pairwise.parse_winner("no verdict") == "TIE"


def test_agreeing_orders_give_a_winner():
    caller = _caller_preferring("good")
    v = pairwise.judge_pair(
        _c("m/x", "+good"),
        _c("m/y", "+bad"),
        task_prompt="p",
        gold=None,
        caller=caller,
        cache=None,
        judge="j",
        rng=random.Random(0),
    )
    assert v.winner == "m/x"
    assert len(caller.calls) == 2  # both orders


def test_position_bias_counts_as_a_tie():
    # Always picks slot A: the two orders disagree, so the verdict is a tie.
    v = pairwise.judge_pair(
        _c("m/x", "+one"),
        _c("m/y", "+two"),
        task_prompt="p",
        gold=None,
        caller=lambda prompt, judge: "WINNER: A",
        cache=None,
        judge="j",
        rng=random.Random(0),
    )
    assert v.winner is None


def test_no_model_judges_its_own_output():
    assert pairwise.pick_judge("q/a", "q/b") == pairwise.DEFAULT_JUDGE
    assert (
        pairwise.pick_judge("anthropic/claude-opus-5.5", "q/b")
        == pairwise.FALLBACK_JUDGE
    )
    assert (
        pairwise.pick_judge("anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5")
        is None
    )


def test_judge_all_skips_pairs_without_an_independent_judge():
    cands = [
        _c("anthropic/claude-opus-5.5", "+good"),
        _c("anthropic/claude-sonnet-5.5", "+bad"),
        _c("q/c", "+meh"),
    ]
    caller = _caller_preferring("good")
    verdicts, skipped = pairwise.judge_all(
        cands, prompts={}, golds={}, caller=caller, cache=None
    )
    assert skipped == 1  # opus vs sonnet
    assert len(verdicts) == 2
    # The opus pair went to the fallback judge, the sonnet pair to the default.
    assert {v.judge for v in verdicts} == {
        pairwise.FALLBACK_JUDGE,
        pairwise.DEFAULT_JUDGE,
    }


def test_cache_makes_reruns_free(tmp_path):
    cache = pairwise.VerdictCache(tmp_path)
    x, y = _c("m/x", "+good"), _c("m/y", "+bad")
    caller = _caller_preferring("good")
    kw = {"task_prompt": "p", "gold": None, "cache": cache, "judge": "j"}
    first = pairwise.judge_pair(x, y, caller=caller, rng=random.Random(0), **kw)
    n = len(caller.calls)
    # Swapped argument order hits the same cache entry.
    again = pairwise.judge_pair(y, x, caller=caller, rng=random.Random(1), **kw)
    assert len(caller.calls) == n
    assert first.winner == again.winner == "m/x"


def test_bradley_terry_orders_models_by_wins():
    verdicts = []
    for _ in range(6):
        verdicts.append(Verdict("t", "strong", "mid", "strong", "j"))
        verdicts.append(Verdict("t", "mid", "weak", "mid", "j"))
        verdicts.append(Verdict("t", "strong", "weak", "strong", "j"))
    verdicts.append(Verdict("t", "mid", "weak", None, "j"))
    r = pairwise.bradley_terry(verdicts)
    assert r["strong"] > r["mid"] > r["weak"]
    assert sum(r.values()) == pytest.approx(0.0, abs=1e-6)


def test_ratings_with_ci_brackets_the_point_estimate():
    verdicts = [Verdict("t", "a", "b", "a", "j")] * 8 + [
        Verdict("t", "a", "b", "b", "j")
    ] * 2
    out = pairwise.ratings_with_ci(verdicts, boot=50)
    lo, hi = out["a"]["judge_ci"]
    assert lo <= out["a"]["judge_rating"] <= hi
    assert out["a"]["judge_rating"] > out["b"]["judge_rating"]
    assert out["a"]["judge_games"] == 10


def test_pick_judge_resolves_ids_through_cli_names():
    # The Sonnet anchor's registry id is not its CLI name; without the map the
    # fallback (Sonnet) would judge its own pair against Opus.
    names = {"anthropic/claude-sonnet-5.5-cc": "claude-sonnet-5-5"}
    assert (
        pairwise.pick_judge(
            "anthropic/claude-sonnet-5.5-cc",
            "anthropic/claude-opus-5.5",
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            names,
        )
        is None
    )
