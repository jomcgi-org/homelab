"""Command line entry point for the session collector."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .collector import parse_session, run_collection
from .render import render
from .scope import discover_repo, parse_allowlist, parse_path_allowlist
from .state import forget, load

DEFAULT_STATE = Path("~/.cache/homelab-tools/session-collector/state.json")
DEFAULT_CLAUDE = Path("~/.claude/projects")
DEFAULT_CODEX = Path("~/.codex/sessions")


def _run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="upload eligible sessions")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet-minutes", type=float, default=30)
    parser.add_argument("--max-uploads", type=int, default=20)
    parser.add_argument("--base-url", default="https://private.jomcgi.dev")
    parser.add_argument("--allow", action="append")
    parser.add_argument("--allow-path", action="append")
    parser.add_argument("--claude-dir", type=Path, default=DEFAULT_CLAUDE)
    parser.add_argument("--codex-dir", type=Path, default=DEFAULT_CODEX)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.session_collector")
    subparsers = parser.add_subparsers(dest="command")
    _run_parser(subparsers)
    status = subparsers.add_parser("status", help="show state counts")
    status.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    inspect_parser = subparsers.add_parser("render", help="render one session")
    inspect_parser.add_argument("path", type=Path)
    inspect_parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    forget_parser = subparsers.add_parser("forget", help="forget one session")
    forget_parser.add_argument("path", type=Path)
    forget_parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0].startswith("-"):
        arguments.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command == "run":
        try:
            allowlist = parse_allowlist(args.allow)
            path_allowlist = parse_path_allowlist(args.allow_path)
        except ValueError as error:
            parser.error(str(error))
        return run_collection(
            claude_dir=args.claude_dir,
            codex_dir=args.codex_dir,
            state_file=args.state_file,
            allowlist=allowlist,
            path_allowlist=path_allowlist,
            quiet_minutes=args.quiet_minutes,
            max_uploads=args.max_uploads,
            base_url=args.base_url,
            dry_run=args.dry_run,
        )
    if args.command == "status":
        entries = load(args.state_file.expanduser()).values()
        counts = Counter(entry.get("status", "unknown") for entry in entries)
        for status in ("uploaded", "skipped", "failed"):
            print(f"{status}: {counts[status]}")
        parked = sum(
            entry.get("status") == "failed" and int(entry.get("failures", 0)) >= 3
            for entry in entries
        )
        print(f"failed (parked): {parked}")
        if counts["unknown"]:
            print(f"unknown: {counts['unknown']}")
        return 0
    if args.command == "render":
        path = args.path.expanduser().resolve()
        state_value = load(args.state_file.expanduser())
        session = parse_session(path)
        repo = discover_repo(session.cwd, state_value)
        scope = f"repo:{repo}" if repo else "repo:unknown"
        print(render(session, repo, scope).markdown, end="")
        return 0
    if args.command == "forget":
        removed = forget(args.state_file.expanduser(), args.path)
        print("forgotten" if removed else "not found")
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
