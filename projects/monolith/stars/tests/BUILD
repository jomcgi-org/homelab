load("@aspect_rules_py//py:defs.bzl", "py_library")

py_library(
    name = "snapshot_fixture",
    testonly = True,
    srcs = ["snapshot_fixture.py"],
    imports = ["../.."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = ["//projects/monolith:pkg_stars"],
)
