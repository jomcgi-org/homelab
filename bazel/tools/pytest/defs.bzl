"Allow rules_python gazelle to generate a macro that runs pytest as the main"

load("@aspect_rules_py//py:defs.bzl", _py_test = "py_test")

# pytest-asyncio 1.x no longer honors `config.option.asyncio_mode` set from a
# plugin's pytest_configure (it reads the mode before third-party plugins
# configure), so the shared testing plugin can no longer flip the repo into
# "auto" mode. Set it here via PYTEST_ADDOPTS `-o`, which pytest parses at
# startup before any plugin runs, so bare `async def` tests are collected
# everywhere. asyncio_default_fixture_loop_scope silences the 1.x deprecation
# that pytest 9 promotes to a collection error. These `-o` keys are only valid
# when pytest-asyncio is installed, so we also ensure every pytest target has it
# in its venv (deduped so gazelle-added copies don't collide).
_ASYNCIO_ADDOPTS = "-o asyncio_mode=auto -o asyncio_default_fixture_loop_scope=function"

def py_test(name, deps = [], **kwargs):
    # Note: Don't add @pip//pytest here - Gazelle adds it from `import pytest` in test files.
    # Adding it here causes duplicate dep errors.

    # Make a mutable copy of deps to avoid "frozen list" error
    mutable_deps = list(deps)

    # pytest-asyncio must be present for the `-o asyncio_mode` option below to be
    # a recognized config key. Gazelle adds it for tests that import it; add it
    # here for the rest, deduped so there is never a duplicate label.
    if "@pip//pytest_asyncio" not in mutable_deps:
        mutable_deps.append("@pip//pytest_asyncio")

    env = dict(kwargs.pop("env", {}))
    prior_addopts = env.get("PYTEST_ADDOPTS", "")
    env["PYTEST_ADDOPTS"] = (prior_addopts + " " + _ASYNCIO_ADDOPTS).strip()

    _py_test(
        name = name,
        pytest_main = True,
        deps = mutable_deps,
        env = env,
        **kwargs
    )
