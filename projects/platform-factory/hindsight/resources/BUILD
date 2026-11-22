# keys.py runs inside the hindsight-keys Jobs (secrets.yaml mounts it from a
# ConfigMap), and preflight.py inside the hindsight-preflight Sync hook
# (hindsight.yaml). Neither is a Bazel target.
# gazelle:exclude bindings.py
# gazelle:exclude keys.py
# gazelle:exclude preflight.py
# apply_template.py runs inside the hindsight-bank-template PostSync Job.
# gazelle:exclude apply_template.py

exports_files(
    ["secrets.yaml"],
    visibility = ["//projects/platform-factory/factory-db:__pkg__"],
)

# hindsight-discord runs a copy; its keys_test keeps the two equal.
exports_files(
    ["preflight.py"],
    visibility = ["//projects/platform-factory/hindsight-discord:__pkg__"],
)

filegroup(
    name = "keys_resources",
    srcs = [
        "apply_template.py",
        "bank-template.json",
        "bindings.py",
        "hindsight.yaml",
        "keys.py",
        "kustomization.yaml",
        "network-policy.yaml",
        "preflight.py",
        "secrets.yaml",
    ],
    visibility = ["//projects/platform-factory/hindsight:__pkg__"],
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
