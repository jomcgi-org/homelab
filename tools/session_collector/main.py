"""Bazel executable wrapper for the session collector."""

from tools.session_collector.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
