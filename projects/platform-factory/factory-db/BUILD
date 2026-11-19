load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "provisioning_test",
    srcs = ["provisioning_test.py"],
    data = [
        "factory/secrets.yaml",
        "//projects/platform-factory/factory-db/resources:provisioning_resources",
        "//projects/platform-factory/hindsight-discord/resources:secrets.yaml",
        "//projects/platform-factory/hindsight/resources:secrets.yaml",
        "//projects/trips/chart:templates/db-secret.yaml",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
