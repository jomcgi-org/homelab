load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(default_visibility = ["//visibility:public"])

exports_files(glob(["*.sh"]))

sh_test(
    name = "dispatch_test",
    srcs = ["dispatch_test.sh"],
    data = [":dispatch.sh"],
)
