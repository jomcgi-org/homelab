load("//bazel/tools/pytest:defs.bzl", "py_test")

# secret_init.py runs inside the kagent-pg-setup Job (setup.yaml mounts it
# from a ConfigMap); it is not a Bazel target.
# gazelle:exclude secret_init.py

exports_files(["claims_test.sql"])

filegroup(
    name = "claims_test_files",
    srcs = [
        "setup.psql",
    ],
    visibility = ["//projects/factory/tests/itsaplan:__pkg__"],
)

py_test(
    name = "setup_test",
    srcs = ["setup_test.py"],
    data = [
        "cluster.yaml",
        "setup.yaml",
    ],
    deps = ["@pip//pyyaml"],
)

exports_files(
    ["network-policy.yaml"],
    visibility = ["//projects/platform-factory/network-policy/tests:__pkg__"],
)

exports_files(
    ["cluster.yaml"],
    visibility = ["//projects/platform-factory/image-audit:__pkg__"],
)

# The image audit renders this directory with kustomize
# (//projects/platform-factory/image-audit:platform_images_test).
filegroup(
    name = "audit_manifests",
    srcs = glob(
        ["**"],
        exclude = [
            "BUILD",
            "BUILD.bazel",
        ],
    ),
    visibility = ["//projects/platform-factory/image-audit:__pkg__"],
)
