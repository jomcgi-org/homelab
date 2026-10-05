"""`estimate` (wall time of a full pass) and `compare` (paired A/B between two runs)."""

import json
from pathlib import Path

from . import stats
from .runner import load_records


def _hours(s):
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


def estimate(args):
    """Projects one full pass on one stream from token counts and engine speed.

    Token counts come from a previous run (--run, per sample), an SGLang metrics file
    (--sgl-metrics, totals), or explicit --prompt-tokens/--completion-tokens per sample.
    With --run, prefill time is the run's measured time to first token (short prompts are
    dominated by a fixed per-request cost, not a tok/s rate) and decode its measured rate;
    --prefill-tps/--decode-tps override them, e.g. to project a faster build.
    """
    items, samples = args.items, args.samples
    prefill_s = decode_tps = None
    if args.run:
        run = Path(args.run)
        cfg = json.loads((run / f"{args.task}.json").read_text())
        recs = [r for r in load_records(run / f"{args.task}.jsonl") if "error" not in r]
        if not recs:
            raise SystemExit(f"no records for {args.task} in {run}")
        n = len(recs)
        prompt = sum(r["prompt_tokens"] - r["cached_tokens"] for r in recs) / n
        completion = sum(r["completion_tokens"] for r in recs) / n
        prefill_s = sum(r["ttft_s"] for r in recs) / n
        decode_tps = sum(r["completion_tokens"] for r in recs) / max(1e-9, sum(r["decode_s"] for r in recs))
        items = items or cfg["dataset_size"]
        samples = samples or cfg["settings"]["samples"]
        print(
            f"{args.task}: {n} measured samples; per sample {prompt:.0f} uncached prompt + {completion:.0f} "
            f"completion tokens, time to first token {prefill_s:.2f} s, decode {decode_tps:.1f} tok/s"
        )
    elif args.sgl_metrics:
        m = json.loads(Path(args.sgl_metrics).read_text())
        n = m["num_examples"] * m.get("n_repeats", 1)
        prompt, completion = m["total_prompt_tokens"] / n, m["total_completion_tokens"] / n
        items = items or m["num_examples"]
        samples = samples or m.get("n_repeats", 1)
        print(f"{m['name']}: {prompt:.0f} prompt + {completion:.0f} completion tokens per sample (from {args.sgl_metrics})")
    else:
        prompt, completion = args.prompt_tokens, args.completion_tokens
        if prompt is None or completion is None:
            raise SystemExit("give --run, --sgl-metrics, or --prompt-tokens and --completion-tokens")
    if args.prefill_tps:
        prefill_s = prompt / args.prefill_tps
    if args.decode_tps:
        decode_tps = args.decode_tps
    if prefill_s is None or not decode_tps or not items:
        raise SystemExit("need --prefill-tps, --decode-tps and --items (or a --run that provides them)")
    samples = samples or 1
    per = prefill_s + completion / decode_tps + args.overhead_s
    total = per * items * samples
    print(
        f"per sample {per:.1f} s: {prefill_s:.1f} s to first token, {completion / decode_tps:.1f} s decode "
        f"at {decode_tps:.1f} tok/s, {args.overhead_s:.1f} s overhead"
    )
    print(f"full pass: {items} items x {samples} samples = {_hours(total)} on one stream")
    if args.arms > 1:
        print(f"{args.arms} arms: {_hours(total * args.arms)}")


def compare(args):
    """Pairs two runs by (item, sample) and reports the score difference B - A per task."""
    a_dir, b_dir = Path(args.a), Path(args.b)
    tasks = args.tasks.split(",") if args.tasks else sorted(
        p.stem for p in a_dir.glob("*.jsonl") if (b_dir / p.name).exists()
    )
    for name in tasks:
        cfg_a = json.loads((a_dir / f"{name}.json").read_text())
        cfg_b = json.loads((b_dir / f"{name}.json").read_text())
        diffs = [k for k in ("settings", "task_args") if cfg_a.get(k) != cfg_b.get(k)]
        ra = {(r["id"], r["sample"]): r for r in load_records(a_dir / f"{name}.jsonl") if "error" not in r}
        rb = {(r["id"], r["sample"]): r for r in load_records(b_dir / f"{name}.jsonl") if "error" not in r}
        keys = sorted(ra.keys() & rb.keys())
        print(f"\n== {name}: {len(keys)} paired samples ({a_dir.name} = A, {b_dir.name} = B)")
        if diffs:
            print(f"   WARNING: {', '.join(diffs)} differ between runs: not a clean A/B")
        if not keys:
            continue
        pa = stats.item_means([ra[k] for k in keys])
        pb = stats.item_means([rb[k] for k in keys])
        ids = sorted(pa)
        a_vals, b_vals = [pa[i] for i in ids], [pb[i] for i in ids]
        ma, mb = sum(a_vals) / len(ids), sum(b_vals) / len(ids)
        lo, hi = stats.paired_bootstrap(a_vals, b_vals, seed=args.seed)
        print(f"   score A {ma * 100:.2f}  B {mb * 100:.2f}  B-A {100 * (mb - ma):+.2f} (95% CI {100 * lo:+.2f} .. {100 * hi:+.2f}, {len(ids)} items)")
        binary = all(ra[k]["score"] in (0.0, 1.0) and rb[k]["score"] in (0.0, 1.0) for k in keys)
        if binary:
            only_a = sum(ra[k]["score"] > rb[k]["score"] for k in keys)
            only_b = sum(rb[k]["score"] > ra[k]["score"] for k in keys)
            same_pred = sum(ra[k].get("pred") == rb[k].get("pred") for k in keys)
            print(
                f"   right only in A {only_a}, only in B {only_b} (McNemar p={stats.mcnemar_exact(only_a, only_b):.3f}); "
                f"same answer {same_pred / len(keys):.1%}"
            )
        groups = sorted({ra[k].get("group") for k in keys} - {None})
        for g in groups:
            gk = [k for k in keys if ra[k].get("group") == g]
            ga = sum(ra[k]["score"] for k in gk) / len(gk)
            gb = sum(rb[k]["score"] for k in gk) / len(gk)
            print(f"   {g:<24} A {ga * 100:6.2f}  B {gb * 100:6.2f}  B-A {100 * (gb - ga):+6.2f}  (n={len(gk)})")

        def side(rs):
            n = len(rs)
            rates = [(r["completion_tokens"] - 1) / r["decode_s"] for r in rs if r["decode_s"] > 0 and r["completion_tokens"] > 1]
            return (
                sum(r["completion_tokens"] for r in rs) / n,
                sum(r["finish_reason"] == "length" for r in rs) / n,
                stats.median([r["ttft_s"] for r in rs]),
                stats.median(rates),
            )

        sa, sb = side([ra[k] for k in keys]), side([rb[k] for k in keys])
        print(f"   completion tokens A {sa[0]:.0f}  B {sb[0]:.0f}   truncated A {sa[1]:.1%}  B {sb[1]:.1%}")
        print(f"   median ttft A {sa[2]:.2f}s  B {sb[2]:.2f}s   median decode A {sa[3]:.1f}  B {sb[3]:.1f} tok/s")
