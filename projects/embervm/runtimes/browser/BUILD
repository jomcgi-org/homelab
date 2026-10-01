# gazelle:ignore
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "evidence_test",
    srcs = [
        "evidence.py",
        "evidence_test.py",
    ],
    imports = ["."],
    deps = ["@pip//pytest"],
)

py_test(
    name = "session_test",
    srcs = [
        "session.py",
        "session_test.py",
    ],
    imports = ["."],
    deps = ["@pip//pytest"],
)

py_test(
    name = "grants_test",
    srcs = [
        "grants.py",
        "grants_test.py",
        "session.py",
    ],
    imports = ["."],
    deps = ["@pip//pytest"],
)
