#!/usr/bin/env python3
"""Prompt-lookup drafting: how much of real output it would draft correctly (#6872).

Two steps:

  run   Sends each workload to an OpenAI-compatible server (temperature 0, thinking
        off) and records the output text, time to first token and decode time.
  sim   Replays prompt-lookup drafting over each recorded output, token by token:
        at each round, the last `n` tokens are matched against the session so far
        (prompt and output generated before them); the tokens that followed the
        most recent match are the draft (up to `k`), and the round accepts the
        longest prefix the real output agrees with, plus the token the target
        produces. With greedy output this is exactly the acceptance the engine
        would see. A cost table (ms per verification step by width, from
        `oominf bench --verify`) turns rounds into a predicted output rate, with
        rounds that find no match falling back to the current decode rate.

Workloads (from files in this repository, so they are reproducible): editing a
file and printing it whole, a unified diff, tests for a module, quoting a
document, and a novel-reasoning control with no input to copy.

Example:
  bench/lookup.py run --url http://127.0.0.1:8091 --out /tmp/lookup
  bench/lookup.py sim --runs /tmp/lookup --tokenizer <model.oom>/tokenizer.json \\
      --cost 1:31,4:71,8:91,12:146,16:213 --fallback-ms-per-token 25.2
"""

import argparse
import json
import pathlib
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text()


def workloads():
    cache = read("crates/oominf-tiers/src/cache.rs")
    host = read("crates/oominf-tiers/src/host.rs")
    grouped = read("crates/oominf-tiers/src/grouped.rs")
    kernel = read("crates/oominf-cpu/src/kernel.rs")
    tiered = read("crates/oominf-tiers/src/tiered.rs")
    arch = read("docs/dev/architecture.md")
    arch = arch[: len(arch) // 3]
    return {
        "edit_rename": (
            "Rename the type `SlotCache` to `ExpertSlots` everywhere in this file, "
            "including doc comments. Output the complete updated file and nothing "
            "else.\n\n```rust\n" + cache + "```"
        ),
        "edit_feature": (
            "Add a public method `in_flight(&self) -> usize` to `DirectReader` that "
            "returns how many submitted reads have not completed, with a doc "
            "comment, next to the other public methods. Output the complete updated "
            "file and nothing else.\n\n```rust\n" + host + "```"
        ),
        "edit_long": (
            "Add a public method `held_records(&self) -> usize` to `TieredExperts` "
            "that returns how many records are currently held for host compute, "
            "with a doc comment, after `vram_bytes`. Output the complete updated "
            "file and nothing else.\n\n```rust\n" + tiered + "```"
        ),
        "diff": (
            "Make `GroupedExperts::describe` prefix each source's description with "
            "its index (`0: ...; 1: ...`). Output only a unified diff against this "
            "file.\n\n```rust\n" + grouped + "```"
        ),
        "tests": (
            "Write additional unit tests for this module covering `nvfp4_rows` with "
            "one token and with `MAX_TOKENS` tokens, and `e4m3` for subnormal "
            "values. Output only the new test functions.\n\n```rust\n" + kernel + "```"
        ),
        "quote": (
            "List the six most important design decisions in this document. For "
            "each, give a one-line summary and then quote, verbatim, the sentence "
            "from the document that justifies it.\n\n" + arch
        ),
        "novel": (
            "Explain to a new engineer, in about 600 words, the trade-offs of "
            "serving a mixture-of-experts model whose experts do not fit in GPU "
            "memory, and how you would decide what to keep resident."
        ),
    }


def stream_chat(url, prompt, max_tokens):
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    first = None
    text, usage = [], None
    with urllib.request.urlopen(req, timeout=3600) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ev = json.loads(line[5:])
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices", []):
                piece = ch.get("delta", {}).get("content")
                if piece:
                    if first is None:
                        first = time.time()
                    text.append(piece)
    end = time.time()
    return {
        "text": "".join(text),
        "ttft_s": (first or end) - t0,
        "decode_s": end - (first or end),
        "usage": usage,
    }


def cmd_run(a):
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, prompt in workloads().items():
        if a.only and name not in a.only:
            continue
        r = stream_chat(a.url, prompt, a.max_tokens)
        r["prompt"] = prompt
        n = (r["usage"] or {}).get("completion_tokens", 0)
        rate = (n - 1) / r["decode_s"] if r["decode_s"] > 0 and n > 1 else 0
        print(
            f"{name:13} {n:5} tokens  ttft {r['ttft_s']:6.1f}s  decode {rate:5.1f} tok/s"
        )
        (out / f"{name}{a.tag}.json").write_text(json.dumps(r))


class Adaptive:
    """Per-sequence draft width from recent lookup acceptance (fraction of drafted
    tokens kept, exponentially weighted): `k` while it is at least `hi`, `mid_k`
    while at least `lo`, else no lookup draft (the model drafts) except every
    `probe`-th match, drafted at `mid_k` to re-measure."""

    def __init__(self, k, hi=0.7, lo=0.35, mid_k=3, alpha=0.3, probe=8):
        self.k, self.hi, self.lo, self.mid_k, self.alpha, self.probe = (
            k,
            hi,
            lo,
            mid_k,
            alpha,
            probe,
        )
        self.rate = 1.0
        self.skipped = 0

    def width(self):
        if self.rate >= self.hi:
            return self.k
        if self.rate >= self.lo:
            return self.mid_k
        self.skipped += 1
        if self.skipped >= self.probe:
            self.skipped = 0
            return self.mid_k
        return 0

    def record(self, drafted, kept):
        if drafted:
            self.rate = (1 - self.alpha) * self.rate + self.alpha * kept / drafted


def simulate(ctx, out, ns, k, adaptive=False):
    """Rounds of prompt lookup over `out` with context `ctx`: (width, kept) each,
    width 0 for rounds with no match (one token at the fallback rate). `ns` are
    the match lengths tried, longest first; each matches its most recent
    occurrence that has a token after it (so the suffix never matches itself)."""
    seq = list(ctx)
    rounds = []
    i = 0
    last = {n: {} for n in ns}
    done = {n: 0 for n in ns}

    def index():
        for n in ns:
            for j in range(done[n], len(seq) - n):
                last[n][tuple(seq[j : j + n])] = j
            done[n] = max(done[n], len(seq) - n)

    index()
    policy = Adaptive(k) if adaptive else None
    while i < len(out):
        draft = []
        width = policy.width() if policy else k
        for n in ns if width else []:
            if len(seq) < n:
                continue
            pos = last[n].get(tuple(seq[-n:]))
            if pos is not None:
                draft = seq[pos + n : pos + n + width]
                break
        if not draft:
            rounds.append((0, 1))
            seq.append(out[i])
            i += 1
            index()
            continue
        kept = 0
        while (
            kept < len(draft) and i + kept < len(out) and out[i + kept] == draft[kept]
        ):
            kept += 1
        take = min(kept + 1, len(out) - i)
        if policy:
            policy.record(len(draft), kept)
        rounds.append((len(draft) + 1, take))
        seq.extend(out[i : i + take])
        i += take
        index()
    return rounds


def cmd_sim(a):
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(a.tokenizer)
    cost = {}
    for part in a.cost.split(","):
        w, ms = part.split(":")
        cost[int(w)] = float(ms)
    widths = sorted(cost)

    def step_ms(width):
        for w in widths:
            if w >= width:
                return cost[w]
        return cost[widths[-1]] * width / widths[-1]

    runs = sorted(pathlib.Path(a.runs).glob("*.json"))
    policies = [("n" + ",".join(map(str, ns)), ns) for ns in a.policy]
    print(
        f"{'workload':13} {'tokens':>6} {'match':>11} {'k':>3}  {'rounds':>6} {'matched':>7} {'tok/round':>9} {'accept':>6}  {'base tok/s':>10} {'pred tok/s':>10} {'gain':>6}"
    )
    for path in runs:
        r = json.loads(path.read_text())
        ctx = tok.encode(r["prompt"]).ids
        out = tok.encode(r["text"]).ids
        n_out = (r.get("usage") or {}).get("completion_tokens", len(out))
        # Fallback: this workload's measured decode rate (current MTP decoding).
        base = (
            (n_out - 1) / r["decode_s"]
            if r["decode_s"] > 0
            else 1e3 / a.fallback_ms_per_token
        )
        fallback_ms = 1e3 / base
        if not out:
            continue
        for label, ns in policies:
            for k in a.draft:
                rounds = simulate(ctx, out, ns, k, a.adaptive)
                ms = 0.0
                drafted = kept_drafts = matched = 0
                for width, take in rounds:
                    if width == 0:
                        ms += fallback_ms
                    else:
                        matched += 1
                        ms += step_ms(width)
                        drafted += width - 1
                        kept_drafts += take - 1
                rate = len(out) / ms * 1e3
                acc = kept_drafts / drafted if drafted else 0
                print(
                    f"{path.stem:13} {len(out):6} {label:>11} {k:3}  {len(rounds):6} {matched:7} "
                    f"{len(out) / len(rounds):9.2f} {acc:6.1%}  {base:10.1f} {rate:10.1f} {rate / base:5.2f}x"
                )


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8091")
    r.add_argument("--out", required=True)
    r.add_argument("--max-tokens", type=int, default=1500)
    r.add_argument("--only", nargs="*")
    r.add_argument("--tag", default="")
    s = sub.add_parser("sim")
    s.add_argument("--runs", required=True)
    s.add_argument("--tokenizer", required=True)
    s.add_argument(
        "--cost", required=True, help="width:ms per verification step, comma separated"
    )
    s.add_argument(
        "--fallback-ms-per-token",
        type=float,
        default=25.0,
        help="when a run has no measured decode rate",
    )
    s.add_argument(
        "--policy",
        type=lambda v: [int(x) for x in v.split(",")],
        nargs="+",
        default=[[3], [8, 6, 4, 3, 2], [8, 6, 4], [8, 6]],
        help="match lengths tried, longest first (comma separated), per policy",
    )
    s.add_argument(
        "--adaptive",
        action="store_true",
        help="adapt the draft width to recent acceptance",
    )
    s.add_argument(
        "--draft",
        type=int,
        nargs="+",
        default=[3, 7, 11, 15],
        help="draft tokens per round (a round verifies one more)",
    )
    a = p.parse_args()
    {"run": cmd_run, "sim": cmd_sim}[a.cmd](a)


if __name__ == "__main__":
    main()
