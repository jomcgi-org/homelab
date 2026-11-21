load("//bazel/tools/pytest:defs.bzl", "py_test")

# compare.py runs from the checkout with npm-installed tools and a host Chromium.
# gazelle:exclude compare.py

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects:__pkg__"],
)

py_test(
    name = "cleanup_test",
    srcs = ["cleanup_test.py"],
    data = ["compare.py"],
    deps = ["@pip//pytest"],
)
