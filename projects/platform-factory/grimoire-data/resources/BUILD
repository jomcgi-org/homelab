# secret_init.py runs inside the grimoire-db-init Job (setup.yaml mounts it
# from a ConfigMap); it is not a Bazel target.
# gazelle:exclude secret_init.py

exports_files(
    ["grants.sql"],
    visibility = ["//projects/grimoire/deploy:__pkg__"],
)

filegroup(
    name = "manifests",
    srcs = [
        "database.yaml",
        "grants.sql",
        "kustomization.yaml",
        "namespace-policy.yaml",
        "policy.yaml",
        "runtime.yaml",
        "secret_init.py",
        "setup.yaml",
        "storage.yaml",
    ],
    visibility = [
        "//projects/platform-factory/grimoire-data:__pkg__",
        "//projects/platform-factory/image-audit:__pkg__",
    ],
)

exports_files(
    ["policy.yaml"],
    visibility = ["//projects/platform-factory/network-policy/tests:__pkg__"],
)

exports_files(
    ["database.yaml"],
    visibility = ["//projects/platform-factory/image-audit:__pkg__"],
)
