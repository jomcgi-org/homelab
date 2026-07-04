load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(default_visibility = ["//visibility:public"])

exports_files(glob(["*.sh"]))

sh_test(
    name = "bump_chart_test",
    srcs = ["bump-chart_test.sh"],
    data = [":bump-chart.sh"],
)
