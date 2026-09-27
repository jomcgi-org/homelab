# gazelle:ignore
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "run_linux_lib",
    srcs = ["run_linux.py"],
    imports = ["."],
)

py_test(
    name = "run_linux_test",
    srcs = ["run_linux_test.py"],
    deps = [
        ":run_linux_lib",
        "@pip//pytest",
    ],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
