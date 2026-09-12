# gazelle:exclude
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:public"])

exports_files(["ci"])

py_library(
    name = "central_build_ratchet_lib",
    srcs = ["central_build_ratchet.py"],
    imports = ["."],
)

py_test(
    name = "central_build_ratchet_test",
    srcs = ["central_build_ratchet_test.py"],
    deps = [
        ":central_build_ratchet_lib",
        "@pip//pytest",
    ],
)

sh_test(
    name = "ci_test",
    srcs = ["ci_test.sh"],
    data = [
        "ci",
        "local-affected-test.sh",
        "//:buildbuddy.yaml",
    ],
)

sh_test(
    name = "affected_targets_test",
    srcs = ["affected_targets_test.sh"],
    data = ["affected-targets.sh"],
)

sh_test(
    name = "local_affected_test_test",
    srcs = ["local_affected_test_test.sh"],
    data = ["local-affected-test.sh"],
)
