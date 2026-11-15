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
