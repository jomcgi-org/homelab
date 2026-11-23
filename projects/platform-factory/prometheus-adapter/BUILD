load("//bazel/tools/pytest:defs.bzl", "py_test")

# The adapter's values query, evaluated by promtool against sample series, and
# the shape of its network policy.
py_test(
    name = "adapter_test",
    srcs = ["adapter_test.py"],
    data = [
        "application.yaml",
        "resources/network-policy.yaml",
        "values.yaml",
        "@promtool_3_15_1_linux_amd64//:binary",
    ],
    env = {"PROMTOOL": "$(rootpath @promtool_3_15_1_linux_amd64//:binary)"},
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
