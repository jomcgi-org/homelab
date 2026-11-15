# garage_init.py runs inside the garage-init Jobs (bootstrap.yaml mounts it
# from a ConfigMap); it is not a Bazel target.
# gazelle:exclude garage_init.py

filegroup(
    name = "backup_resources",
    srcs = [
        "garage_init.py",
        "network-policy.yaml",
    ],
    visibility = ["//projects/platform-factory/authentik/resources:__pkg__"],
)
