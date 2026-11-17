# keys.py runs inside the hindsight-keys Jobs (secrets.yaml mounts it from a
# ConfigMap); it is not a Bazel target.
# gazelle:exclude keys.py

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
        "secrets.yaml",
    ],
    visibility = ["//projects/platform-factory/hindsight:__pkg__"],
)
