"""oomeval: benchmark an OpenAI-compatible server and compare runs.

uv run python -m oomeval list
uv run python -m oomeval run --label bf16 --tasks gsm8k --limit 300 --temperature 0
uv run python -m oomeval estimate --run results/bf16 --task gsm8k --decode-tps 43
uv run python -m oomeval compare results/bf16 results/fp8
"""

import argparse
import time
from pathlib import Path

from . import client, report, runner
from .tasks import TASKS


def add_run_args(p):
    p.add_argument("--url", default="http://127.0.0.1:8091", help="server base URL")
    p.add_argument("--model", default="oominf", help="model name sent in requests")
    p.add_argument(
        "--label", required=True, help="run name; results go to <out>/<label>/"
    )
    p.add_argument("--out", default="results", help="results directory")
    p.add_argument(
        "--notes", default="", help="free text stored with the run (build, flags)"
    )
    p.add_argument("--tasks", required=True, help=f"comma-separated: {','.join(TASKS)}")
    p.add_argument("--limit", type=int, help="seeded random subset of N items per task")
    p.add_argument("--subset-seed", type=int, default=0)
    s = p.add_argument_group(
        "sampling (defaults follow each task's published protocol)"
    )
    s.add_argument("--temperature", type=float)
    s.add_argument("--top-p", type=float)
    s.add_argument("--top-k", type=int)
    s.add_argument("--presence-penalty", type=float)
    s.add_argument("--max-tokens", type=int)
    s.add_argument(
        "--samples", type=int, help="samples per item (pass@1 is their mean)"
    )
    s.add_argument(
        "--seed", type=int, default=0, help="request seed = seed + sample index"
    )
    s.add_argument(
        "--thinking",
        choices=("on", "off", "default"),
        default="default",
        help="chat template enable_thinking; 'default' keeps the task's setting or the template's",
    )
    s.add_argument("--reasoning-effort")
    e = p.add_argument_group("execution")
    e.add_argument(
        "--timeout", type=float, default=4 * 3600, help="per-request timeout, seconds"
    )
    e.add_argument(
        "--time-budget",
        type=float,
        help="stop cleanly after this many minutes (rerun to resume)",
    )
    e.add_argument(
        "--ready-timeout",
        type=float,
        default=900,
        help="seconds to wait for the server to load",
    )
    e.add_argument("--max-failures", type=int, default=5)
    e.add_argument(
        "--keep-reasoning", action="store_true", help="store reasoning text in records"
    )
    e.add_argument("--progress-every", type=int, default=10)
    e.add_argument("--verbose", action="store_true")
    for t in TASKS.values():
        t.add_args(p)


def main():
    ap = argparse.ArgumentParser(
        prog="oomeval",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="tasks, their default protocol and published scores")

    add_run_args(sub.add_parser("run", help="run tasks (resumes an existing label)"))

    sm = sub.add_parser("summary", help="re-print a run's summaries")
    sm.add_argument("run")

    es = sub.add_parser("estimate", help="project the wall time of a full pass")
    es.add_argument(
        "--run", help="results/<label> to take per-sample tokens and rates from"
    )
    es.add_argument("--task", help="task within --run")
    es.add_argument("--sgl-metrics", help="SGLang eval metrics JSON (token totals)")
    es.add_argument(
        "--prompt-tokens", type=float, help="uncached prompt tokens per sample"
    )
    es.add_argument(
        "--completion-tokens",
        type=float,
        help="completion tokens per sample (reasoning included)",
    )
    es.add_argument(
        "--items", type=int, help="items in a full pass (default: the dataset size)"
    )
    es.add_argument("--samples", type=int, help="samples per item")
    es.add_argument(
        "--prefill-tps",
        type=float,
        help="prefill tok/s (default: the time to first token measured in --run)",
    )
    es.add_argument(
        "--decode-tps", type=float, help="decode tok/s (default: measured in --run)"
    )
    es.add_argument(
        "--overhead-s", type=float, default=0.0, help="fixed seconds per request"
    )
    es.add_argument("--arms", type=int, default=1, help="number of settings to compare")

    cp = sub.add_parser("compare", help="paired comparison of two runs (B - A)")
    cp.add_argument("a")
    cp.add_argument("b")
    cp.add_argument("--tasks", help="comma-separated; default every task in both")
    cp.add_argument("--seed", type=int, default=0, help="bootstrap seed")

    args = ap.parse_args()
    if args.cmd == "list":
        for t in TASKS.values():
            print(f"{t.name:<14} {t.description}")
            print(f"{'':<14} defaults {t.defaults}")
            for p in t.published:
                print(
                    f"{'':<14} published {p.value} {p.metric}: {p.protocol} [{p.source}]"
                )
    elif args.cmd == "run":
        names = [n.strip() for n in args.tasks.split(",") if n.strip()]
        unknown = [n for n in names if n not in TASKS]
        if unknown:
            raise SystemExit(f"unknown tasks {unknown}; choose from {list(TASKS)}")
        if not client.wait_ready(args.url, args.ready_timeout):
            raise SystemExit(f"{args.url} not ready after {args.ready_timeout:.0f} s")
        deadline = (
            time.monotonic() + args.time_budget * 60 if args.time_budget else None
        )
        for n in names:
            task = TASKS[n]()
            finished = runner.run_task(task, args, deadline)
            runner.summarize(task, Path(args.out) / args.label)
            if not finished:
                break
    elif args.cmd == "summary":
        run = Path(args.run)
        for p in sorted(run.glob("*.jsonl")):
            if p.stem in TASKS:
                runner.summarize(TASKS[p.stem](), run)
    elif args.cmd == "estimate":
        if args.run and not args.task:
            raise SystemExit("--run needs --task")
        report.estimate(args)
    elif args.cmd == "compare":
        report.compare(args)


if __name__ == "__main__":
    main()
