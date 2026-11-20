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
    visibility = ["//projects/platform-factory/grimoire-data:__pkg__"],
)
