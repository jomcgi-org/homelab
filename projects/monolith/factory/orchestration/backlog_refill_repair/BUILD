# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

# Shared controls supplies the existing models and refill policy closure.
# Writer imports stay lazy so execution does not depend on the full swarm.
py_library(
    name = "backlog_refill_repair",
    srcs = ["__init__.py"],
    imports = ["../../.."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = ["@pip//sqlmodel"],
)
