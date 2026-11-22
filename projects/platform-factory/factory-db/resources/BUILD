# secret_init.py runs inside the factory-db Jobs (setup.yaml mounts it from a
# ConfigMap); it is not a Bazel target.
# gazelle:exclude secret_init.py

filegroup(
    name = "provisioning_resources",
    srcs = [
        "cluster.yaml",
        "network-policy.yaml",
        "secret_init.py",
        "setup.yaml",
    ],
    visibility = ["//projects/platform-factory/factory-db:__pkg__"],
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
