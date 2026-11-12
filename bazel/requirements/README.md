# Python requirements

Third-party Python packages for the whole repo, compiled with uv through
`rules_uv` into one lock file that `pip.parse` turns into the `@pip` hub.

| File | Purpose | Edited by |
| --- | --- | --- |
| `//:pyproject.toml` (`dependencies`) | Runtime dependencies, loose constraints | hand |
| `test.in` | Test-only dependencies, constrained by `-c runtime.txt` | hand |
| `overrides.txt` | Security floors forced onto transitive packages | hand |
| `runtime.txt` | Pinned runtime lock | `:runtime` |
| `all.in` | Aggregates `test.in` and `runtime.txt` | rarely |
| `all.txt` | Pinned lock for everything; the input to `pip.parse` in `MODULE.bazel` | `:requirements.all` |

Runtime compiles first and constrains the test layer, so a test dependency
cannot move a runtime pin.

## Changing a dependency

1. Edit `pyproject.toml`, `test.in` or `overrides.txt`.
2. Regenerate both locks:

   ```bash
   bazel/tools/format/update-python-requirements.sh
   ```

   It runs `//bazel/requirements:runtime` then
   `//bazel/requirements:requirements.all`. Both need internet access and are
   tagged `no-remote`.

3. Reference the package as `@pip//package_name`: underscores in Bazel,
   hyphens on PyPI. `requirement()` is not used in this repo.

Both scripts need a local Bazel. The `Update Python requirements` pre-commit
hook runs the sibling `update-python-requirements-if-needed.sh`, which
regenerates and stages the locks when `:runtime_test` fails, but only when
`pyproject.toml` changes. An edit to `test.in` or `overrides.txt`
alone stays inert until the script is run by hand.

Drop an `overrides.txt` floor once the upstream constraint floats past it.
