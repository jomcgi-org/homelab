load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "restore_test",
    size = "medium",
    srcs = ["restore_test.py"],
    data = [
        "inventory.sql",
        "restore.sh",
        "//projects/platform-factory/grimoire-data/resources:grants.sql",
        "@postgres_test//:postgres",
    ],
    env = {
        "GRANTS_SQL": "$(rootpath //projects/platform-factory/grimoire-data/resources:grants.sql)",
        "INVENTORY_SQL": "$(rootpath inventory.sql)",
        "RESTORE_SH": "$(rootpath restore.sh)",
    },
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = ["@pip//pytest"],
)

filegroup(
    name = "manifests",
    srcs = [
        "kustomization.yaml",
        "recovery.yaml",
        "restore.sh",
        "restore-cronjob.yaml",
    ],
    visibility = ["//projects/platform-factory/grimoire-data:__pkg__"],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
