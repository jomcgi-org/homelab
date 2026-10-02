"""Macro for domain BDD test targets with shared harness pre-wired."""

load("//bazel/tools/pytest:defs.bzl", "py_test")

# No `playwright` knob any more (issue #4219). It used to add the frontend_dist
# data dep plus `playwright` and `manual` tags, and `manual` is what kept the
# browser specs out of wildcard expansion: they never ran anywhere, so the
# tests themselves were deleted rather than left implying coverage. Re-adding a
# browser lane means solving hermetic Chromium for the Bazel sandbox first.
def bdd_test(name, srcs, future = False, size = "large", timeout = "moderate", **kwargs):
    """BDD test target with shared testing fixtures and data deps.

    Args:
        name: Target name.
        srcs: Test source files (include the domain's tests/conftest.py).
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
