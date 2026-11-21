"""Score independently adjudicated historical reviewer replays."""

import argparse
import json
from pathlib import Path


def rate(label, hits, total):
    value = f"{hits / total:.1%}" if total else "unavailable"
    print(f"{label}: {value} ({hits}/{total})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--split", choices=("scored", "tuning"), default="scored")
    args = parser.parse_args()
    cases = json.loads(Path(__file__).with_name("cases.json").read_text())
    results = json.loads(args.results.read_text())
    agent = check = blocks = defects = controls = 0
    for case in cases:
        if case["split"] != args.split:
            continue
        head = case["head"]
        row = results.get(head, {})
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
        failed = row["conclusion"] == "failure"
        if case["answer"] is None:
            controls += 1
            blocks += failed
        else:
            defects += 1
            agent += matched is not None
            # invariant: FACT-13
            check += failed and matched is not None and matched in blocking
    rate("Agent finding recall", agent, defects)
    rate("Check conclusion recall", check, defects)
    rate("Control false-block rate", blocks, controls)


if __name__ == "__main__":
    main()
