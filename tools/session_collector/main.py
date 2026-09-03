"""Bazel executable wrapper for the session collector."""

from tools.session_collector.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
