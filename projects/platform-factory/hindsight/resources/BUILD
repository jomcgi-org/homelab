# keys.py runs inside the hindsight-keys Jobs (secrets.yaml mounts it from a
# ConfigMap), and preflight.py inside the hindsight-preflight Sync hook
# (hindsight.yaml). Neither is a Bazel target.
# gazelle:exclude keys.py
# gazelle:exclude preflight.py
# apply_template.py runs inside the hindsight-bank-template PostSync Job.
# gazelle:exclude apply_template.py

exports_files(
    ["secrets.yaml"],
    visibility = ["//projects/platform-factory/factory-db:__pkg__"],
)

filegroup(
    name = "keys_resources",
    srcs = [
        "apply_template.py",
        "bank-template.json",
        "hindsight.yaml",
        "keys.py",
        "kustomization.yaml",
        "network-policy.yaml",
        "preflight.py",
        "secrets.yaml",
    ],
    visibility = ["//projects/platform-factory/hindsight:__pkg__"],
)
