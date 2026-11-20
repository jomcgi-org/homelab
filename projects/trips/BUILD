exports_files(
    ["approvals.sql"],
    visibility = [
        "//projects/trips/export:__pkg__",
        "//projects/trips/ingest:__pkg__",
    ],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
