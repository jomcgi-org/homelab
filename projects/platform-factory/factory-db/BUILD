load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "provisioning_test",
    srcs = ["provisioning_test.py"],
    data = [
        "factory/secrets.yaml",
        "//projects/platform-factory/factory-db/resources:provisioning_resources",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
