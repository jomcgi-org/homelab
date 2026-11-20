exports_files(
    [
        "application.yaml",
        "checks/allowed.yaml",
        "checks/denied.yaml",
        "values.yaml",
    ],
    visibility = ["//projects/platform-factory/external-secrets:__subpackages__"],
)

# This package boundary excludes the README from //projects:repo_docs.
filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
