# gazelle:ignore
# The parent owns the runnable tests and private runtime dependency closure.
# This package boundary also keeps dormant Loom code out of public source globs.
filegroup(
    name = "sources",
    srcs = glob(
        ["*.py"],
        exclude = ["*_test.py"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

exports_files([
    "client_test.py",
    "ontology_test.py",
    "chart_test.py",
])
