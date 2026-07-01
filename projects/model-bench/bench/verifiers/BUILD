load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle populates this file with py_library / py_test targets for the verifiers
# subpackage. Committed empty so CI's gazelle modifies a tracked file that the
# ci-format-bot auto-commits via `git add -u` (a new untracked BUILD would not be).

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "command_semgrep_test",
    srcs = ["command.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "compile_semgrep_test",
    srcs = ["compile.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "effect_verifiers_test_semgrep_test",
    srcs = ["effect_verifiers_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "helm_semgrep_test",
    srcs = ["helm.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "jsonmatch_semgrep_test",
    srcs = ["jsonmatch.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "lint_semgrep_test",
    srcs = ["lint.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "rbac_semgrep_test",
    srcs = ["rbac.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "verifiers_test_semgrep_test",
    srcs = ["verifiers_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

py_library(
    name = "verifiers",
    srcs = [
        "__init__.py",
        "command.py",
        "compile.py",
        "helm.py",
        "jsonmatch.py",
        "lint.py",
        "rbac.py",
    ],
    imports = ["../.."],
    visibility = ["//projects/model-bench:__subpackages__"],
    deps = [
        "//projects/model-bench/bench",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "effect_verifiers_test",
    srcs = ["effect_verifiers_test.py"],
    imports = ["../.."],
    deps = [":verifiers"],
)

py_test(
    name = "verifiers_test",
    srcs = ["verifiers_test.py"],
    imports = ["../.."],
    deps = [
        ":verifiers",
        "@pip//pytest",
    ],
)
