# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# This boundary keeps the shared handoff context out of ancestor source globs.
py_library(
    name = "workspace_handoff",
    srcs = ["__init__.py"],
    imports = ["../../.."],
    visibility = ["//projects/monolith:__pkg__"],
)
