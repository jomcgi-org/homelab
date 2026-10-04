#!/usr/bin/env python3
"""Writes a long, realistic prompt of about N tokens for prefill benchmarks.

The text is the repository's own Markdown (design docs, ADRs, READMEs) in path
order, cut at the target length, then a question about it.
Prose and code routed through real experts measure prefill honestly; repeated
filler would route every token to the same few experts.

Assumes about 3.75 characters per token; `oominf bench` prints the exact count.

Example:
  bench/make_prompt.py --tokens 32768 --out /tmp/prompt-32k.txt
  oominf bench --model <model.oom> --prompt-file /tmp/prompt-32k.txt
"""

import argparse
import subprocess
import sys
from pathlib import Path

CHARS_PER_TOKEN = 3.75
QUESTION = "\n\nSummarise the main design decisions in these documents in five bullet points."


def corpus(root):
    files = subprocess.run(
        ["git", "-C", root, "ls-files", "docs/*.md", "projects/*.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    for f in sorted(files):
        try:
            yield f, (Path(root) / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tokens", type=int, required=True, help="approximate prompt length")
    p.add_argument("--out", required=True)
    p.add_argument("--root", default=None, help="repository root (default: this checkout)")
    args = p.parse_args()
    root = args.root or subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
    ).stdout.strip()
    budget = int(args.tokens * CHARS_PER_TOKEN) - len(QUESTION)
    parts, used = [], 0
    for name, text in corpus(root):
        doc = f"=== {name} ===\n{text.strip()}\n\n"
        if used + len(doc) > budget:
            doc = doc[: budget - used]
        parts.append(doc)
        used += len(doc)
        if used >= budget:
            break
    if used < budget:
        sys.exit(f"corpus holds only about {used / CHARS_PER_TOKEN:.0f} tokens")
    Path(args.out).write_text("".join(parts) + QUESTION, encoding="utf-8")


if __name__ == "__main__":
    main()
