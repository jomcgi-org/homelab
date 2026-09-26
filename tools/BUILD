load("@aspect_rules_py//py:defs.bzl", "py_library")

exports_files(
    ["__init__.py"],
    visibility = ["//bazel/tools/image:__pkg__"],
)

py_library(
    name = "tools",
    srcs = ["__init__.py"],
    visibility = ["//:__subpackages__"],
)
