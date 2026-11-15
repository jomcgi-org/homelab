# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# Composed by shared controls, including in the pruned execution closure.
# Do not pull the repository-capable funding writer into ordinary sessions.
py_library(
    name = "funding_profile",
    srcs = ["__init__.py"],
    imports = ["../../.."],
    visibility = ["//projects/monolith:__pkg__"],
)
