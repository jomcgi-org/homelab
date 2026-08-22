# gazelle:exclude
load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(default_visibility = ["//visibility:public"])

exports_files(["ci"])

sh_test(
    name = "ci_test",
    srcs = ["ci_test.sh"],
    data = [
        "ci",
        "//:buildbuddy.yaml",
    ],
)

sh_test(
    name = "affected_targets_test",
    srcs = ["affected_targets_test.sh"],
    data = ["affected-targets.sh"],
)
