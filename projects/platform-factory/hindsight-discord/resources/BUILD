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
