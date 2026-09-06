"""Validate pinned semgrep-core output before any finding-only filtering.

Contract: Semgrep 1.168.0, semgrep_output_v1.atd core_output/core_match.
CLI output and historical stats.okfiles are not the core protocol.
"""

import json
from pathlib import Path
import sys


class InvalidOutput(ValueError):
    """The scanner did not prove a complete successful scan."""


def require(condition, message):
    if not condition:
        raise InvalidOutput(message)


def nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def read_output(path):
    with path.open() as stream:
        data = json.load(stream)
    require(isinstance(data, dict), "core output must be an object")
    require(nonempty_string(data.get("version")), "missing or invalid version")
    require(isinstance(data.get("results"), list), "missing or invalid results")
    require(isinstance(data.get("errors"), list), "missing or invalid errors")
    require(not data["errors"], f"engine reported {len(data['errors'])} error(s)")
    paths = data.get("paths")
    require(isinstance(paths, dict), "missing or invalid paths")
    require(isinstance(paths.get("scanned"), list), "missing or invalid paths.scanned")
    require(all(nonempty_string(p) for p in paths["scanned"]), "invalid scanned path")
    if "skipped_rules" in data:
        require(isinstance(data["skipped_rules"], list), "invalid skipped_rules")
        require(not data["skipped_rules"], "engine skipped invalid rules")
    for match in data["results"]:
        require(isinstance(match, dict), "result must be an object")
        for key in ("check_id", "path"):
            require(nonempty_string(match.get(key)), f"missing or invalid result {key}")
        for key in ("start", "end"):
            position = match.get(key)
            require(isinstance(position, dict), f"missing or invalid result {key}")
            for field in ("line", "col") + (
                ("offset",) if "offset" in position else ()
            ):
                value = position.get(field)
                require(
                    type(value) is int and value >= (-1 if field == "offset" else 1),
                    f"invalid result {key}.{field}",
                )
        extra = match.get("extra")
        require(isinstance(extra, dict), "missing or invalid result extra")
        if "message" in extra:
            require(isinstance(extra["message"], str), "invalid result extra.message")
        require(
            isinstance(extra.get("metavars"), dict), "invalid result extra.metavars"
        )
        require(
            type(extra.get("is_ignored")) is bool, "invalid result extra.is_ignored"
        )
        kind = extra.get("engine_kind")
        tagged_pro = (
            isinstance(kind, list)
            and len(kind) == 2
            and kind[0] == "PRO_REQUIRED"
            and isinstance(kind[1], dict)
            and all(
                type(kind[1].get(key)) is bool
                for key in (
                    "interproc_taint",
                    "interfile_taint",
                    "proprietary_language",
                )
            )
        )
        require(
            kind in ("OSS", "PRO") or tagged_pro, "invalid result extra.engine_kind"
        )
    return data


def normalized(path, scan_dir):
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(scan_dir).as_posix()
        except ValueError:
            pass
    return candidate.as_posix()


def merge(results_dir, output, expected_count, scan_dir):
    files = sorted(results_dir.glob("result_*.json"))
    require(expected_count > 0, "no engine passes were attempted")
    require(len(files) == expected_count, "missing engine pass output")
    merged = {"results": [], "errors": [], "paths": {"scanned": []}}
    scanned = set()
    for path in files:
        data = read_output(path)
        merged["version"] = data["version"]
        scanned.update(normalized(p, scan_dir) for p in data["paths"]["scanned"])
        for match in data["results"]:
            match["path"] = normalized(match["path"], scan_dir)
            merged["results"].append(match)
    # Empty individual passes are valid for rule/language intersections. The
    # complete target must still have engine-confirmed work, including SCA.
    require(scanned, "zero engine-confirmed scanned paths across all passes")
    merged["paths"]["scanned"] = sorted(scanned)
    output.write_text(json.dumps(merged))
    print(
        f"SCANNED: {len(scanned)} distinct engine-confirmed path(s), {len(files)} pass(es)"
    )
    return int(bool(merged["results"]))


def finish(path, excluded):
    data = read_output(path)
    original = data["results"]
    data["results"] = [
        match
        for match in original
        if not any(
            match["check_id"] == rule or match["check_id"].endswith("." + rule)
            for rule in excluded
        )
    ]
    path.write_text(json.dumps(data))
    if len(data["results"]) != len(original):
        print(f"EXCLUDED: {len(original) - len(data['results'])} finding(s) by rule ID")
    for match in data["results"]:
        print(f"  {match['check_id']} at {match['path']}:{match['start']['line']}")
        if match["extra"].get("message"):
            print(f"    {match['extra']['message'][:200]}")
    if data["results"]:
        print(f"FAILED: Semgrep found violations ({len(data['results'])} finding(s))")
        return 1
    print("PASSED: No semgrep findings")
    return 0


def main(args):
    try:
        if args[0] == "validate":
            data = read_output(Path(args[1]))
            print(
                f"OUTPUT: scanned={len(set(data['paths']['scanned']))} findings={len(data['results'])}"
            )
            return 0
        if args[0] == "merge":
            return merge(Path(args[1]), Path(args[2]), int(args[3]), Path(args[4]))
        if args[0] == "finish":
            return finish(Path(args[1]), args[2:])
        raise InvalidOutput("unknown output operation")
    except (InvalidOutput, OSError, ValueError, TypeError, KeyError) as error:
        print(f"INFRASTRUCTURE: {str(error)[:500]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
