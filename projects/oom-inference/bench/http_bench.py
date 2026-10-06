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

Example:
  bench/http_bench.py --url http://127.0.0.1:8091 --runs 3
  bench/http_bench.py --url http://127.0.0.1:8090 --model qwen3.6-27b --cases short
"""

import argparse
import json
import statistics
import sys
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
    if first is None:
        raise RuntimeError("no tokens received")
    tokens = (usage or {}).get("completion_tokens") or chunks
    decode = (
        (tokens - 1) / (last - first) if tokens > 1 and last > first else float("nan")
    )
    cached = ((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens")
    return {
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
    args = p.parse_args()

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
