load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "restore_test",
    size = "medium",
    srcs = ["restore_test.py"],
    data = [
        "inventory.sql",
        "restore.sh",
        "//projects/monolith/chart:files/grimoire-inventory.sql",
        "//projects/monolith/chart:files/grimoire-migration-dump.sh",
        "//projects/platform-factory/grimoire-data/resources:grants.sql",
        "@postgres_test//:postgres",
    ],
    env = {
        "GRANTS_SQL": "$(rootpath //projects/platform-factory/grimoire-data/resources:grants.sql)",
        "HUB_DUMP_SH": "$(rootpath //projects/monolith/chart:files/grimoire-migration-dump.sh)",
        "HUB_INVENTORY_SQL": "$(rootpath //projects/monolith/chart:files/grimoire-inventory.sql)",
        "INVENTORY_SQL": "$(rootpath inventory.sql)",
        "RESTORE_SH": "$(rootpath restore.sh)",
    },
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = ["@pip//pytest"],
)

# The hub dump job's chart test pins its copy of inventory.sql and its
# pg_dump image to these.
exports_files(
    [
        "inventory.sql",
        "restore-cronjob.yaml",
    ],
    visibility = ["//projects/monolith/chart:__pkg__"],
)

filegroup(
    name = "manifests",
    srcs = [
        "inventory.sql",
        "kustomization.yaml",
        "migration.sh",
        "migration-check-cronjob.yaml",
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
