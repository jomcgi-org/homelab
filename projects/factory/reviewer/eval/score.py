"""Score independently adjudicated historical reviewer replays."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def rate(label, hits, total):
    value = f"{hits / total:.1%}" if total else "unavailable"
    print(f"{label}: {value} ({hits}/{total})")


def replay(executable, case):
    base, head = case["base"], case["head"]
    diff = subprocess.check_output(["git", "diff", "--no-ext-diff", base, head, "--"])
    result = subprocess.run(
        [executable, base, head], input=diff, stdout=subprocess.PIPE, check=True
    )
    row = json.loads(result.stdout)
    row["base"] = base
    row["matched_id"] = None
    if case["answer"] is not None and row["findings"]:
        print(json.dumps(row, indent=2), case["answer"], sep="\n", file=sys.stderr)
        print(
            "Matching finding ID (empty for miss): ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        row["matched_id"] = input().strip() or None
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("results", nargs="?", type=Path)
    source.add_argument("--reviewer", metavar="EXECUTABLE")
    parser.add_argument("--split", choices=("scored", "tuning"), default="scored")
    parser.add_argument("--output", type=Path, help="save per-head replay evidence")
    args = parser.parse_args()
    if args.output and not args.reviewer:
        parser.error("--output requires --reviewer")
    output = args.output or Path("results.json")
    if args.reviewer and (output.exists() or output.is_symlink()):
        parser.error(f"evidence destination already exists: {output}")
    cases = json.loads(Path(__file__).with_name("cases.json").read_text())
    results = json.loads(args.results.read_text()) if args.results else {}
    agent = check = blocks = defects = controls = 0
    for case in cases:
        if case["split"] != args.split:
            continue
        head = case["head"]
        row = replay(args.reviewer, case) if args.reviewer else results.get(head, {})
        if row.get("base") != case["base"] or row.get("conclusion") not in (
            "failure",
            "success",
        ):
            parser.error(f"missing or incomplete replay for {head}")
        matched = row["matched_id"]
        findings = {f["id"] for f in row["findings"]}
        blocking = set(row["blocking_ids"])
        if matched is not None and (not matched or matched not in findings):
            parser.error(f"unknown matched finding on {head}")
        results[head] = row
        failed = row["conclusion"] == "failure"
        if case["answer"] is None:
            controls += 1
            blocks += failed
        else:
            defects += 1
            agent += matched is not None
            # invariant: FACT-14
            check += failed and matched is not None and matched in blocking
    if args.reviewer:
        with output.open("x", encoding="utf-8") as evidence:
            json.dump(results, evidence, indent=2)
            evidence.write("\n")
    rate("Agent finding recall", agent, defects)
    rate("Check conclusion recall", check, defects)
    rate("Control false-block rate", blocks, controls)


if __name__ == "__main__":
    main()
