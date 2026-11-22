load("//bazel/tools/pytest:defs.bzl", "py_test")

# secret_init.py runs inside the secret-init Job (secret-init.yaml mounts it
# from a ConfigMap); it is not a Bazel target.
# gazelle:exclude secret_init.py

py_test(
    name = "backup_test",
    srcs = ["backup_test.py"],
    data = [
        "backup-key-wait.yaml",
        "kustomization.yaml",
        "network-policy.yaml",
        "//projects/platform-factory/factory-buckets/resources:backup_resources",
        "//projects/platform-factory/garage/resources:backup_resources",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

exports_files(
    ["network-policy.yaml"],
    visibility = ["//projects/platform-factory/network-policy/tests:__pkg__"],
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
