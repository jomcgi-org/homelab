#!/usr/bin/env python3
"""Time-to-first-token and decode speed of an OpenAI-compatible chat endpoint.

Streams chat completions with fixed prompts and reports, per run and as medians:
TTFT (request to first generated token) and decode tok/s (completion tokens after
the first, over the time from first to last token). Standard library only.

Every run (warm-ups included) uses a different topic, so no run re-measures
text whose routed experts an earlier run already made resident.

Cases:
  short       a short chat prompt
  long        a ~2k-token prompt (prefill-bound TTFT)
  multiturn   a follow-up that extends the previous turn verbatim (prefix reuse)

Concurrency mode (--concurrency K1,K2,...): for each K, K streaming requests with
different prompts (different tasks and topics), fixed max_tokens, temperature 0,
start together; per round it reports aggregate output tok/s (all completion
tokens over the time from the first request's start to the last token),
per-stream decode tok/s, TTFT, and p50/p95 inter-token latency (the gaps between
streamed chunks of every request; drafts accepted together arrive together).
With --long-every N, every Nth request of a round carries the ~2k-token prompt
instead, to measure how much a prefill stalls the others (max gap).

Example:
  bench/http_bench.py --url http://127.0.0.1:8091 --runs 3
  bench/http_bench.py --url http://127.0.0.1:8090 --model qwen3.6-27b --cases short
  bench/http_bench.py --url http://127.0.0.1:8091 --concurrency 1,2,4,8 --rounds 2
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request

TOPICS = [
    "a lighthouse keeper",
    "a glacier survey team",
    "a night-shift baker",
    "a deep-sea cable repair",
    "a desert observatory",
    "a river ferry pilot",
    "a beekeeper in winter",
    "a clockmaker's apprentice",
    "a mountain rescue dog",
    "a lunar greenhouse",
    "a railway signal box",
    "a museum restorer",
    "a storm-chasing meteorologist",
    "a lost-and-found office",
    "a violin maker",
    "an orbital debris tracker",
]

NOTES = [
    "The pump station on the east bank failed twice in March after the spring thaw.",
    "Crews replaced the impeller and found sediment packed around the intake screen.",
    "Upstream logging increased the silt load, according to the river authority survey.",
    "A second intake with a coarse pre-filter was proposed but not yet budgeted.",
    "Night shifts reported pressure drops of ten to fifteen percent before each failure.",
    "The telemetry system logs pressure every five minutes, which hides short spikes.",
    "Operators suggested logging every ten seconds during the thaw season only.",
    "The maintenance contract expires in September and renewal terms are under review.",
]


def long_prompt(target_words=1500):
    words, lines, i = 0, [], 0
    while words < target_words:
        line = f"Note {i + 1}: {NOTES[i % len(NOTES)]}"
        lines.append(line)
        words += len(line.split())
        i += 1
    return "Summarize these maintenance notes in three bullet points.\n\n" + "\n".join(
        lines
    )


def stream_chat(url, model, messages, max_tokens, timeout):
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    first = last = None
    chunks = 0
    times = []
    usage = None
    reasoning, content = [], []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("reasoning_content") or delta.get("reasoning") or ""
                reasoning.append(text)
                content.append(delta.get("content") or "")
                if text or delta.get("content") or delta.get("tool_calls"):
                    now = time.perf_counter()
                    first = first if first is not None else now
                    last = now
                    chunks += 1
                    times.append(now)
    if first is None:
        raise RuntimeError("no tokens received")
    tokens = (usage or {}).get("completion_tokens") or chunks
    decode = (
        (tokens - 1) / (last - first) if tokens > 1 and last > first else float("nan")
    )
    cached = ((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens")
    return {
        "start": t0,
        "times": times,
        "ttft": first - t0,
        "decode_tps": decode,
        "tokens": tokens,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "cached_tokens": cached,
        "reasoning": "".join(reasoning),
        "content": "".join(content),
    }


def run_case(args, case, run):
    """`run` numbers every run of every case (warm-ups first), so each gets its own topic."""
    topic = TOPICS[run % len(TOPICS)]
    if case == "short":
        prompt = f"Write a short story about {topic}."
        return stream_chat(
            args.url,
            args.model,
            [{"role": "user", "content": prompt}],
            args.max_tokens,
            args.timeout,
        )
    if case == "long":
        prompt = f"(For a report on {topic}.) " + long_prompt()
        return stream_chat(
            args.url,
            args.model,
            [{"role": "user", "content": prompt}],
            64,
            args.timeout,
        )
    if case == "multiturn":
        first_turn = [
            {
                "role": "user",
                "content": f"Name three challenges facing {topic}, briefly.",
            }
        ]
        r1 = stream_chat(
            args.url, args.model, first_turn, args.max_tokens, args.timeout
        )
        reply = {"role": "assistant", "content": r1["content"]}
        if r1["reasoning"]:
            reply["reasoning_content"] = r1["reasoning"]
        follow = first_turn + [
            reply,
            {"role": "user", "content": "Which of those matters most today, and why?"},
        ]
        return stream_chat(args.url, args.model, follow, args.max_tokens, args.timeout)
    raise ValueError(case)


TASKS = [
    "Write a long, detailed story about {}.",
    "Explain step by step how {} would plan a difficult week, with numbered lists.",
    "Write a Python program that simulates the daily work of {}, with comments.",
    "Write a long poem in rhyming couplets about {}.",
    "Draft a detailed technical report on the equipment used by {}.",
    "Give a long list of interview questions for {}, each with a model answer.",
    "Describe the history and future of the job of {} in a long essay.",
    "Write a play script with three characters set around {}.",
]


def percentile(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return xs[i]


def concurrent_round(args, k, round_no):
    """K requests started together; returns the round's summary."""
    results = [None] * k
    errors = []

    def one(i):
        n = round_no * 64 + i
        if args.long_every and i % args.long_every == args.long_every - 1:
            prompt = f"(For a report on {TOPICS[n % len(TOPICS)]}.) " + long_prompt()
        else:
            prompt = TASKS[n % len(TASKS)].format(TOPICS[(n // len(TASKS) + n) % len(TOPICS)])
        try:
            results[i] = stream_chat(args.url, args.model, [{"role": "user", "content": prompt}], args.max_tokens, args.timeout)
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(f"request {i}: {e}")

    threads = [threading.Thread(target=one, args=(i,)) for i in range(k)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise RuntimeError("; ".join(errors))
    start = min(r["start"] for r in results)
    end = max(r["times"][-1] for r in results)
    tokens = sum(r["tokens"] for r in results)
    gaps = [b - a for r in results for a, b in zip(r["times"], r["times"][1:])]
    return {
        "k": k,
        "aggregate_tps": tokens / (end - start),
        "tokens": tokens,
        "wall": end - start,
        "stream_tps": statistics.median(r["decode_tps"] for r in results),
        "ttft_p50": percentile([r["ttft"] for r in results], 0.5),
        "ttft_max": max(r["ttft"] for r in results),
        "itl_p50": percentile(gaps, 0.5),
        "itl_p95": percentile(gaps, 0.95),
        "itl_max": max(gaps) if gaps else float("nan"),
    }


def concurrency_mode(args):
    rows = []
    for k in [int(x) for x in args.concurrency.split(",")]:
        for w in range(args.warmup):
            concurrent_round(args, k, 1000 + w)
        runs = []
        for r in range(args.rounds):
            s = concurrent_round(args, k, k * 10 + r)
            runs.append(s)
            print(
                f"K={k:2d} round {r}: aggregate {s['aggregate_tps']:6.1f} tok/s  per stream {s['stream_tps']:5.1f} tok/s  "
                f"ttft p50 {s['ttft_p50']:5.2f}s max {s['ttft_max']:5.2f}s  itl p50 {1e3 * s['itl_p50']:6.1f} ms "
                f"p95 {1e3 * s['itl_p95']:6.1f} ms max {1e3 * s['itl_max']:7.1f} ms  ({s['tokens']} tokens in {s['wall']:.1f}s)",
                flush=True,
            )
        med = {key: statistics.median(r[key] for r in runs) for key in runs[0] if key != "k"}
        med["k"] = k
        rows.append(med)
    print("\n| K | aggregate tok/s | per-stream tok/s | TTFT p50 (s) | TTFT max (s) | ITL p50 (ms) | ITL p95 (ms) | ITL max (ms) |")
    print("|---|---|---|---|---|---|---|---|")
    for m in rows:
        print(
            f"| {m['k']} | {m['aggregate_tps']:.1f} | {m['stream_tps']:.1f} | {m['ttft_p50']:.2f} | {m['ttft_max']:.2f} | "
            f"{1e3 * m['itl_p50']:.1f} | {1e3 * m['itl_p95']:.1f} | {1e3 * m['itl_max']:.0f} |"
        )
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"url": args.url, "concurrency": rows}, f, indent=2)
    return 0


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", default="http://127.0.0.1:8091")
    p.add_argument("--model", default="default", help="model name sent in requests")
    p.add_argument("--cases", default="short,long,multiturn")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument(
        "--warmup", type=int, default=1, help="untimed runs of each case first"
    )
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--json", help="also write results to this file")
    p.add_argument("--concurrency", help="concurrency mode: comma-separated request counts, e.g. 1,2,4,8")
    p.add_argument("--rounds", type=int, default=2, help="concurrency mode: timed rounds per count")
    p.add_argument("--long-every", type=int, default=0, help="concurrency mode: every Nth request has the long prompt")
    args = p.parse_args()
    if args.concurrency:
        return concurrency_mode(args)

    results = {}
    serial = 0
    for case in args.cases.split(","):
        for _ in range(args.warmup):
            run_case(args, case, serial)
            serial += 1
        runs = []
        for i in range(args.runs):
            r = run_case(args, case, serial)
            serial += 1
            runs.append(r)
            print(
                f"{case:9s} run {i}: ttft {r['ttft']:6.2f}s  decode {r['decode_tps']:6.2f} tok/s  "
                f"tokens {r['tokens']}  prompt {r['prompt_tokens']} (cached {r['cached_tokens']})",
                flush=True,
            )
        med = {
            "ttft": statistics.median(r["ttft"] for r in runs),
            "decode_tps": statistics.median(r["decode_tps"] for r in runs),
        }
        print(
            f"{case:9s} median: ttft {med['ttft']:.2f}s  decode {med['decode_tps']:.2f} tok/s",
            flush=True,
        )
        results[case] = {
            "median": med,
            "runs": [
                {k: v for k, v in r.items() if k not in ("reasoning", "content")}
                for r in runs
            ],
        }
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"url": args.url, "results": results}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
