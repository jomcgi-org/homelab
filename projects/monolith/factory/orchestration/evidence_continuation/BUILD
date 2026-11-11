# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# Shared controls composes this source with its existing model/API closure.
# The package boundary keeps it out of the ancestor's implementation glob.
py_library(
    name = "evidence_continuation",
    srcs = ["__init__.py"],
    imports = ["../../.."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)
