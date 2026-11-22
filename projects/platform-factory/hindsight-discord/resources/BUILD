# keys.py runs inside the hindsight-keys Jobs (secrets.yaml mounts it from a
# ConfigMap), and preflight.py inside the hindsight-preflight Sync hook
# (hindsight.yaml). Neither is a Bazel target.
# gazelle:exclude keys.py
# gazelle:exclude preflight.py

exports_files(
    ["secrets.yaml"],
    visibility = ["//projects/platform-factory/factory-db:__pkg__"],
)

filegroup(
    name = "keys_resources",
    srcs = [
        "hindsight.yaml",
        "keys.py",
        "kustomization.yaml",
        "network-policy.yaml",
        "preflight.py",
        "secrets.yaml",
        "storage.yaml",
    ],
    visibility = ["//projects/platform-factory/hindsight-discord:__pkg__"],
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
