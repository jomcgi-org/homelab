load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle populates this file with py_library / py_test targets for the verifiers
# subpackage. Committed empty so CI's gazelle modifies a tracked file that the
# ci-format-bot auto-commits via `git add -u` (a new untracked BUILD would not be).

py_library(
    name = "verifiers",
    srcs = [
        "__init__.py",
        "command.py",
        "compile.py",
        "helm.py",
        "jsonmatch.py",
        "lint.py",
        "pytest.py",
        "rbac.py",
        "sandbox.py",
    ],
    imports = ["../.."],
    visibility = ["//projects/model-bench:__subpackages__"],
    deps = ["@pip//pyyaml"],
)

py_test(
    name = "effect_verifiers_test",
    srcs = ["effect_verifiers_test.py"],
    imports = ["../.."],
    deps = [
        ":verifiers",
        "@pip//pytest",
    ],
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

py_test(
    name = "sandbox_test",
    srcs = ["sandbox_test.py"],
    imports = ["../.."],
    deps = [
        ":verifiers",
        "@pip//pytest",
    ],
)
