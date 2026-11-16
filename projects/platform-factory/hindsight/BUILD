load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "keys_test",
    srcs = ["keys_test.py"],
    data = [
        "agents/secrets.yaml",
        "//projects/platform-factory/hindsight/resources:keys_resources",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
