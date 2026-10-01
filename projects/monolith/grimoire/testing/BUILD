# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# A Bazel package boundary excludes these helpers from every ancestor glob,
# including the private, public, jobs, and domain-image production closures.
py_library(
    name = "pkg_grimoire_testing",
    testonly = True,
    srcs = [
        "__init__.py",
        "audience_matrix.py",
    ],
    imports = ["../.."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        "//projects/monolith:pkg_grimoire",
        "@pip//sqlalchemy",
    ],
)
