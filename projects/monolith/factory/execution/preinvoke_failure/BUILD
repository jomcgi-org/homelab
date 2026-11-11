# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# Pure evidence vocabulary shared by admission reconciliation and execution.
# A local package keeps it out of the ancestor execution source glob.
py_library(
    name = "preinvoke_failure",
    srcs = ["__init__.py"],
    imports = ["../../.."],
    visibility = ["//projects/monolith:__pkg__"],
)
