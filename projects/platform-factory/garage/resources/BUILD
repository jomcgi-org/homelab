# garage_init.py runs inside the garage-init Jobs (bootstrap.yaml mounts it
# from a ConfigMap); it is not a Bazel target.
# gazelle:exclude garage_init.py

load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(
    [
        "bootstrap.yaml",
        "claim-admission.yaml",
    ],
    visibility = ["//projects/factory-public/admission:__pkg__"],
)

filegroup(
    name = "backup_resources",
    srcs = [
        "garage_init.py",
        "network-policy.yaml",
    ],
    visibility = ["//projects/platform-factory/authentik/resources:__pkg__"],
)

py_test(
    name = "garage_init_test",
    srcs = ["garage_init_test.py"],
    data = [
        "bootstrap.yaml",
        "garage_init.py",
        "network-policy.yaml",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
