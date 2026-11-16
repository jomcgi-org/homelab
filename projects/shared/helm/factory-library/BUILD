load("//bazel/tools/pytest:defs.bzl", "py_test")

# Expose this package's README to the repository documentation aggregate.
filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//:__pkg__"],
)

py_test(
    name = "library_test",
    srcs = ["library_test.py"],
    data = [
        "//projects/shared/helm/factory-library/chart",
        "@multitool//tools/helm",
    ] + glob(["goldens/**"]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
