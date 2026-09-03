load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "session_collector",
    srcs = [
        "__init__.py",
        "claude_v1.py",
        "codex_v1.py",
        "collector.py",
        "main.py",
        "models.py",
        "redact.py",
        "render.py",
        "scope.py",
        "state.py",
        "upload.py",
    ],
    imports = ["../.."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":session_collector_bin",
        "//tools/cli",
        "@pip//httpx",
    ],
)

py_venv_binary(
    name = "main",
    srcs = ["main.py"],
    main = "main.py",
    deps = [":session_collector"],
)

py_test(
    name = "adapters_test",
    srcs = ["adapters_test.py"],
    data = [
        "fixtures/claude.jsonl",
        "fixtures/codex.jsonl",
    ],
    imports = ["../.."],
    deps = [":session_collector"],
)

py_test(
    name = "redact_test",
    srcs = ["redact_test.py"],
    data = [
        "fixtures/claude.jsonl",
        "fixtures/codex.jsonl",
    ],
    imports = ["../.."],
    deps = [
        ":session_collector",
        "@pip//httpx",
    ],
)

py_test(
    name = "state_test",
    srcs = ["state_test.py"],
    data = [
        "install.sh",
        "launchd/dev.jomcgi.session-collector.plist",
    ],
    imports = ["../.."],
    deps = [
        ":session_collector",
        ":session_collector_bin",
    ],
)

py_test(
    name = "scope_test",
    srcs = ["scope_test.py"],
    imports = ["../.."],
    deps = [":session_collector"],
)

py_test(
    name = "upload_test",
    srcs = ["upload_test.py"],
    imports = ["../.."],
    deps = [
        ":session_collector",
        "//tools/cli",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

semgrep_target_test(
    name = "main_semgrep_test",
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":main",
)

semgrep_test(
    name = "adapters_test_semgrep_test",
    srcs = ["adapters_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "redact_test_semgrep_test",
    srcs = ["redact_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "scope_test_semgrep_test",
    srcs = ["scope_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "state_test_semgrep_test",
    srcs = ["state_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "upload_test_semgrep_test",
    srcs = ["upload_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

py_venv_binary(
    name = "session_collector_bin",
    srcs = ["__main__.py"],
    main = "__main__.py",
    visibility = ["//:__subpackages__"],
)
