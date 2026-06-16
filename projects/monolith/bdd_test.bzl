"""Macro for domain BDD test targets with shared harness pre-wired."""

load("//bazel/tools/pytest:defs.bzl", "py_test")

def bdd_test(name, srcs, playwright = False, future = False, size = "large", timeout = "moderate", **kwargs):
    """BDD test target with shared testing fixtures and data deps.

    Args:
        name: Target name.
        srcs: Test source files (include the domain's tests/conftest.py).
        playwright: If True, adds frontend_dist data dep and playwright tag.
        future: If True, tags the target `future`: an executable spec for a
            feature that is not built yet. The gating `Test` CI action excludes
            `-future`, so a red future spec can merge; the non-required
            `BDD future features` action runs only `future`-tagged specs. When
            the feature lands and the spec passes, drop `future = True` to
            promote it back into the gating suite.
        size: Test size (default "large" since it starts real PostgreSQL).
        timeout: Test timeout (default "moderate").
        **kwargs: Passed to py_test.
    """
    data = [
        "//projects/monolith/chart:migrations",
        "@postgres_test//:postgres",
    ]
    tags = ["bdd"]

    if future:
        # Not `manual`: the future lane selects these via --test_tag_filters=future
        # over //..., and `manual` would drop them from wildcard expansion.
        tags.append("future")

    if playwright:
        data.append("//projects/monolith:frontend_dist")
        tags.append("playwright")
        tags.append("manual")  # playwright dep not yet in pip lockfile

    # Register the shared testing plugin via env var instead of conftest.py.
    # pytest rootdir in Bazel is _main (workspace root), so a conftest.py
    # inside projects/monolith/ is "non-top-level" and rejected by pytest 8.x.
    env = kwargs.pop("env", {})
    env.setdefault("PYTEST_ADDOPTS", "-p shared.testing.plugin")

    py_test(
        name = name,
        srcs = srcs,
        data = data,
        imports = ["."],
        tags = tags,
        size = size,
        timeout = timeout,
        env = env,
        deps = [
            "//projects/monolith:shared_testing",
            "//projects/monolith:monolith_backend",
        ] + kwargs.pop("deps", []),
        **kwargs
    )
