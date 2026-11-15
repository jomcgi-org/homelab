exports_files(
    [
        "application.yaml",
        "values.yaml",
    ],
    visibility = ["//projects/platform-factory/external-secrets/chart:__pkg__"],
)

# This package boundary excludes the README from //projects:repo_docs.
filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
