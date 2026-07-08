load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle populates this file with py_library / py_test targets for the bench package.
# It is committed empty (rather than left for gazelle to create fresh) so that CI's
# gazelle run modifies a tracked file, which the ci-format-bot then auto-commits via
# `git add -u`. A brand-new untracked BUILD would be generated in CI but never committed.

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "cache_semgrep_test",
    srcs = ["cache.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "cache_test_semgrep_test",
    srcs = ["cache_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "claude_code_semgrep_test",
    srcs = ["claude_code.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "claude_code_test_semgrep_test",
    srcs = ["claude_code_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "cli_semgrep_test",
    srcs = ["cli.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "cli_test_semgrep_test",
    srcs = ["cli_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "judge_semgrep_test",
    srcs = ["judge.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "judge_test_semgrep_test",
    srcs = ["judge_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "openrouter_semgrep_test",
    srcs = ["openrouter.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "openrouter_test_semgrep_test",
    srcs = ["openrouter_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "pareto_semgrep_test",
    srcs = ["pareto.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "pareto_test_semgrep_test",
    srcs = ["pareto_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "registry_semgrep_test",
    srcs = ["registry.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "registry_test_semgrep_test",
    srcs = ["registry_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "report_semgrep_test",
    srcs = ["report.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "report_test_semgrep_test",
    srcs = ["report_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "runner_semgrep_test",
    srcs = ["runner.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "runner_test_semgrep_test",
    srcs = ["runner_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "schema_semgrep_test",
    srcs = ["schema.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "schema_test_semgrep_test",
    srcs = ["schema_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

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
    deps = [":bench"],
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

semgrep_test(
    name = "agent_semgrep_test",
    srcs = ["agent.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "agent_test_semgrep_test",
    srcs = ["agent_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
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
