# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# This package boundary excludes the entire private collector from the public
# and serving source globs. Only the jobs binary depends on the runtime library.
py_library(
    name = "sepa",
    srcs = [
        "__init__.py",
        "collector.py",
    ],
    imports = [".."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        "//projects/monolith:pkg_core",
        "@pip//botocore",
        "@pip//httpx",
        "@pip//pyarrow",
    ],
)

exports_files(
    ["collector_test.py"],
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "fixtures",
    srcs = glob(["fixtures/*.json"]),
    visibility = ["//projects/monolith:__pkg__"],
)
