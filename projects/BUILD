load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

py_library(
    name = "projects",
    srcs = ["__init__.py"],
    visibility = ["//:__subpackages__"],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

# The cluster enrollment roots intentionally have no package-local BUILD files.
# Expose only the two hand-maintained kustomizations the Renovate migration test
# needs, without turning either root into a Gazelle package.
filegroup(
    name = "renovate_enrollment_manifests",
    srcs = [
        "platform-gke/kustomization.yaml",
        "platform/kustomization.yaml",
    ],
    visibility = ["//projects/platform/renovate:__pkg__"],
)
