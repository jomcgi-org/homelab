"""Runs tasks against a server, one request at a time, appending one JSON record per sample.

Results live in <out>/<label>/<task>.jsonl with the run's settings in <task>.json. A rerun with
the same settings resumes: completed (item, sample) pairs are skipped, failed ones retried.
"""

import hashlib
import json
import random
import sys
import time
from pathlib import Path

from . import client, stats

SAMPLING_KEYS = ("temperature", "top_p", "top_k", "presence_penalty", "max_tokens")


def resolve_settings(task, args):
    """Task defaults overridden by any CLI value that was given."""
    s = {"samples": 1, **task.defaults}
    for k in (*SAMPLING_KEYS, "samples"):
        v = getattr(args, k)
        if v is not None:
            s[k] = v
    if args.thinking != "default":
        s["thinking"] = args.thinking == "on"
    if args.reasoning_effort:
        s["reasoning_effort"] = args.reasoning_effort
    s["seed"] = args.seed
    return s


def task_args(task, args):
    """The task's own CLI arguments (named --<task>-...), as recorded settings."""
    prefix = task.name + "_"
    return {k: v for k, v in vars(args).items() if k.startswith(prefix)}


def select(items, limit, subset_seed):
    """A seeded random subset in original order, so every arm sees the same items."""
    if not limit or limit >= len(items):
        return items
    keep = set(random.Random(subset_seed).sample(range(len(items)), limit))
    return [it for i, it in enumerate(items) if i in keep]


def request_body(item, settings, sample, model):
    body = {
        "model": model,
        "messages": item.messages,
        "seed": settings["seed"] + sample,
    }
    for k in SAMPLING_KEYS:
        if k in settings:
            body[k] = settings[k]
    kwargs = {}
    if "thinking" in settings:
        kwargs["enable_thinking"] = settings["thinking"]
    if "reasoning_effort" in settings:
        kwargs["reasoning_effort"] = settings["reasoning_effort"]
    if kwargs:
        body["chat_template_kwargs"] = kwargs
    return body


def load_records(path):
    if not path.exists():
        return []
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def run_task(task, args, deadline):
    items = task.load(args)
    dataset_size = len(items)
    items = select(items, args.limit, args.subset_seed)
    settings = resolve_settings(task, args)
    config = {
        "task": task.name,
        "settings": settings,
        "task_args": task_args(task, args),
        "limit": args.limit,
        "subset_seed": args.subset_seed,
        "items_sha": hashlib.sha256(
            json.dumps([it.id for it in items]).encode()
        ).hexdigest()[:16],
        "dataset_size": dataset_size,
    }
    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)
    cfg_path, rec_path = out / f"{task.name}.json", out / f"{task.name}.jsonl"
    if cfg_path.exists():
        prev = json.loads(cfg_path.read_text())
        if {k: prev.get(k) for k in config} != config:
            sys.exit(
                f"{cfg_path} was written with different settings; use a new --label "
                f"(previous: {json.dumps({k: prev.get(k) for k in config})})"
            )
    else:
        meta = {
            "url": args.url,
            "model": args.model,
            "notes": args.notes,
            "server_models": client.models(args.url),
        }
        cfg_path.write_text(
            json.dumps(
                {**config, **meta, "started": time.strftime("%Y-%m-%dT%H:%M:%S")},
                indent=2,
            )
        )

    done = {(r["id"], r["sample"]) for r in load_records(rec_path) if "error" not in r}
    todo = [
        (it, s)
        for it in items
        for s in range(settings["samples"])
        if (it.id, s) not in done
    ]
    print(
        f"[{task.name}] {len(items)} items x {settings['samples']} samples; {len(todo)} to run",
        flush=True,
    )
    failures = 0
    with rec_path.open("a") as f:
        for n, (item, sample) in enumerate(todo, 1):
            if deadline and time.monotonic() > deadline:
                print(
                    f"[{task.name}] time budget reached; rerun the same command to resume",
                    flush=True,
                )
                return False
            record = {"id": item.id, "sample": sample, "group": item.meta.get("group")}
            try:
                c = client.chat(
                    args.url,
                    request_body(item, settings, sample, args.model),
                    args.timeout,
                )
            except Exception as e:  # recorded and retried on resume
                failures += 1
                record["error"] = repr(e)
                f.write(json.dumps(record) + "\n")
                f.flush()
                print(f"[{task.name}] {item.id}/{sample} failed: {e!r}", flush=True)
                if failures >= args.max_failures:
                    sys.exit(f"[{task.name}] {failures} failures; stopping")
                continue
            score, pred = task.score(item, c.content)
            record.update(
                score=score,
                pred=pred,
                target=item.target,
                finish_reason=c.finish_reason,
                prompt_tokens=c.prompt_tokens,
                cached_tokens=c.cached_tokens,
                completion_tokens=c.completion_tokens,
                ttft_s=round(c.ttft_s, 4),
                decode_s=round(c.decode_s, 4),
                wall_s=round(c.wall_s, 4),
                content=c.content,
                reasoning_chars=len(c.reasoning),
            )
            if args.keep_reasoning:
                record["reasoning"] = c.reasoning
            f.write(json.dumps(record) + "\n")
            f.flush()
            if args.verbose or n % max(1, args.progress_every) == 0:
                print(
                    f"[{task.name}] {n}/{len(todo)} {item.id}/{sample} score={score:.2f} pred={pred!r} "
                    f"tok={c.prompt_tokens}+{c.completion_tokens} {c.wall_s:.1f}s",
                    flush=True,
                )
    return True


def summarize(task, out_dir):
    """Prints and writes <task>.summary.json from the records; returns the summary."""
    cfg = json.loads((out_dir / f"{task.name}.json").read_text())
    records = [
        r for r in load_records(out_dir / f"{task.name}.jsonl") if "error" not in r
    ]
    if not records:
        print(f"[{task.name}] no records")
        return None
    mean, half = stats.mean_ci(list(stats.item_means(records).values()))
    groups = {}
    for r in records:
        if r.get("group"):
            groups.setdefault(r["group"], []).append(r)
    rates = [
        (r["completion_tokens"] - 1) / r["decode_s"]
        for r in records
        if r["decode_s"] > 0 and r["completion_tokens"] > 1
    ]
    summary = {
        "task": task.name,
        "items": len({r["id"] for r in records}),
        "samples": len(records),
        "score": mean * 100,
        "ci95": half * 100,
        "groups": {
            g: stats.mean_ci(list(stats.item_means(rs).values()))[0] * 100
            for g, rs in sorted(groups.items())
        },
        "truncated_rate": sum(r["finish_reason"] == "length" for r in records)
        / len(records),
        "mean_prompt_tokens": sum(r["prompt_tokens"] for r in records) / len(records),
        "mean_completion_tokens": sum(r["completion_tokens"] for r in records)
        / len(records),
        "median_ttft_s": stats.median([r["ttft_s"] for r in records]),
        "median_decode_tps": stats.median(rates),
        "total_wall_h": sum(r["wall_s"] for r in records) / 3600,
        "dataset_size": cfg["dataset_size"],
        "settings": cfg["settings"],
    }
    (out_dir / f"{task.name}.summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"\n== {task.name}: {summary['score']:.2f} ± {summary['ci95']:.2f} "
        f"({summary['items']} items, {summary['samples']} samples of {cfg['dataset_size']} items)"
    )
    for g, v in summary["groups"].items():
        print(f"   {g:<24} {v:6.2f}")
    print(
        f"   truncated {summary['truncated_rate']:.1%}  tokens {summary['mean_prompt_tokens']:.0f} in / "
        f"{summary['mean_completion_tokens']:.0f} out  ttft {summary['median_ttft_s']:.2f}s  "
        f"decode {summary['median_decode_tps']:.1f} tok/s  wall {summary['total_wall_h']:.2f} h"
    )
    for p in task.published:
        print(f"   published {p.value:.2f} {p.metric}: {p.protocol} [{p.source}]")
    return summary
