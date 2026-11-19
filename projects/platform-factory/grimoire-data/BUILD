load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "provisioning_test",
    srcs = ["provisioning_test.py"],
    data = [
        "//projects/grimoire/deploy:manifests",
        "//projects/platform-factory/garage/resources:backup_resources",
        "//projects/platform-factory/grimoire-data/resources:manifests",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
