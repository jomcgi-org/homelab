# This tool imports itself as the top-level `bench` package (for example
# `from bench.sandbox import run_sandboxed`), and it lives in a subdirectory whose
# name (`model-bench`) is not a valid Python module path. Mark this directory as the
# Python import root so gazelle resolves first-party `bench.*` imports to targets
# under `//projects/model-bench/bench/...` instead of looking for `bench/` at the
# repo root. Without this, gazelle fails dependency validation for every intra-package
# import. gazelle generates the py_library / py_test targets below this line.
# gazelle:python_root
