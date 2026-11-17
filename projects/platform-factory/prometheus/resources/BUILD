load("@rules_shell//shell:sh_test.bzl", "sh_test")

# The alert rules' unit tests: promtool evaluates rules.yml against the
# series in rules_test.yml. pr-checks runs it when any of its inputs
# changes (bazel/tools/ci/affected-targets.sh).
sh_test(
    name = "rules_test",
    srcs = ["rules_test.sh"],
    args = [
        "$(rootpath @promtool_3_15_1_linux_amd64//:binary)",
        "$(rootpath rules_test.yml)",
    ],
    data = [
        "rules.yml",
        "rules_test.yml",
        "@promtool_3_15_1_linux_amd64//:binary",
    ],
)
