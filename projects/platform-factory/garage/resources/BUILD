# garage_init.py runs inside the garage-init Jobs (bootstrap.yaml mounts it
# from a ConfigMap); it is not a Bazel target.
# gazelle:exclude garage_init.py

load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(
    [
        "bootstrap.yaml",
        "claim-admission.yaml",
    ],
    visibility = ["//projects/shared/helm/factory-library/admission:__pkg__"],
)

filegroup(
    name = "backup_resources",
    srcs = [
        "garage_init.py",
        "network-policy.yaml",
    ],
    visibility = [
        "//projects/platform-factory/authentik/resources:__pkg__",
        "//projects/platform-factory/grimoire-data:__pkg__",
    ],
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

exports_files(
    ["garage.yaml"],
    visibility = ["//projects/platform-factory/priority:__pkg__"],
)
