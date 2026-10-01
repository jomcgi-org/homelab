load("//bazel/tools/pytest:defs.bzl", "py_test")

MANIFESTS = [
    "cnpg-cluster.yaml",
    "cnpg-metrics-service.yaml",
    "cnpg-scheduledbackup.yaml",
    "namespace.yaml",
    "onepassworditem-cnpg-backup-gcs.yaml",
]

exports_files(
    MANIFESTS,
    visibility = ["//bazel/helm:__pkg__"],
)

py_test(
    name = "deploy_manifests_test",
    srcs = ["deploy_manifests_test.py"],
    data = MANIFESTS + [
        "README.md",
        "kustomization.yaml",
        "//bazel/images:generate-home-cluster",
    ],
    env = {
        "HOME_CLUSTER_GENERATOR_BIN": "$(rootpath //bazel/images:generate-home-cluster)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# Markdown read by the monolith doc-manifest genrules (#6446).
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
