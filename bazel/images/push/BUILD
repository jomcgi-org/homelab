load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Its own package rather than bazel/images/: that BUILD is regenerated wholesale
# by //bazel/images:generate-push-all, and validate-generate-scripts.sh diffs
# every "//..." label in it against `bazel query`, so a test target there would
# be both overwritten and read as drift.

exports_files(["push-changed.sh"])

sh_binary(
    name = "push_changed",
    srcs = ["push-changed.sh"],
    data = [
        "private_harness_package.py",
        "//bazel/helm:verify-published-images.sh",
        "//bazel/tools/ci:oci_push_layout.py",
    ],
    visibility = ["//visibility:public"],
)

sh_test(
    name = "push_changed_test",
    srcs = ["push-changed_test.sh"],
    data = [":push-changed.sh"],
)

py_test(
    name = "private_harness_package_test",
    srcs = [
        "private_harness_package.py",
        "private_harness_package_test.py",
    ],
    deps = ["@pip//pytest"],
)
