# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# A Bazel package boundary keeps this helper out of :shared_testing, which
# pulls :monolith_backend into every test that depends on it.
py_library(
    name = "sqlite",
    testonly = True,
    srcs = ["__init__.py"],
    imports = ["../../.."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)
