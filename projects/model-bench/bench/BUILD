load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle populates this file with py_library / py_test targets for the bench package.
# It is committed empty (rather than left for gazelle to create fresh) so that CI's
# gazelle run modifies a tracked file, which the ci-format-bot then auto-commits via
# `git add -u`. A brand-new untracked BUILD would be generated in CI but never committed.

py_library(
    name = "bench",
    srcs = [
        "__init__.py",
        "agent.py",
        "cache.py",
        "claude_code.py",
        "cli.py",
        "judge.py",
        "openrouter.py",
        "pareto.py",
        "registry.py",
        "report.py",
        "runner.py",
        "schema.py",
    ],
    imports = [".."],
    visibility = ["//projects/model-bench:__subpackages__"],
    deps = [
        "//projects/model-bench/bench/verifiers",
        "@pip//httpx",
        "@pip//pydantic",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "cache_test",
    srcs = ["cache_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "claude_code_test",
    srcs = ["claude_code_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "cli_test",
    srcs = ["cli_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "judge_test",
    srcs = ["judge_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "openrouter_test",
    srcs = ["openrouter_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "pareto_test",
    srcs = ["pareto_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "registry_test",
    srcs = ["registry_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "report_test",
    srcs = ["report_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "runner_test",
    srcs = ["runner_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "//projects/model-bench/bench/verifiers",
        "@pip//pytest",
    ],
)

py_test(
    name = "schema_test",
    srcs = ["schema_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_test",
    srcs = ["agent_test.py"],
    imports = [".."],
    deps = [
        ":bench",
        "//projects/model-bench/bench/verifiers",
        "@pip//pytest",
    ],
)
