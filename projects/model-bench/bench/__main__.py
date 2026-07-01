"""Package entry point so the tool runs via `python -m bench` (vendored python).

This shim exists instead of an `if __name__ == "__main__"` guard in cli.py because
gazelle would auto-generate a py_binary for a guarded module, and this repo maps
py_binary to aspect's py_venv_binary, which omits gazelle's implicit `main` default
and fails to load. The file is excluded from gazelle (see ../BUILD) and is not part
of the Bazel py_library; it is only used for the manual, billed model runs.
"""

from bench.cli import main

main()
