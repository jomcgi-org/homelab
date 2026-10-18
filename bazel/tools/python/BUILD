"""Python manifest generation and isolated Linux drift checks.

Ordinary test discovery excludes this package. The explicit integrity test
does not depend on modules_map; updates and generated-byte checks fetch wheels.
"""

load("@bazel_skylib//rules:diff_test.bzl", "diff_test")
load("@pip//:requirements.bzl", "all_whl_requirements")
load("@rules_python_gazelle_plugin//manifest:defs.bzl", "gazelle_python_manifest")
load("@rules_python_gazelle_plugin//modules_mapping:def.bzl", "modules_mapping")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

# Fetches metadata for python packages we depend on.
modules_mapping(
    name = "modules_map",
    wheels = all_whl_requirements,
)

# Provide a mapping from an import to the installed package that provides it.
# Needed to generate BUILD files for .py files.
# This macro produces two targets:
# - //bazel/tools/python:gazelle_python_manifest.update can be used with `bazel run`
#   to recalculate the manifest
# - //bazel/tools/python:gazelle_python_manifest.test is a test target ensuring that
#   the manifest doesn't need to be updated
gazelle_python_manifest(
    name = "gazelle_python_manifest",
    modules_mapping = ":modules_map",
    pip_repository_name = "pip",
    requirements = "//bazel/requirements:all.txt",
)

# Only run this all-wheel comparison when the manifest's inputs change.
diff_test(
    name = "manifest_generated_test",
    file1 = ":gazelle_python_manifest.generated_manifest",
    file2 = "gazelle_python.yaml",
    tags = ["manual"],
)

# Exercise the same pinned integrity test with valid and deliberately stale
# inputs, without building the modules mapping or modifying the checkout.
sh_test(
    name = "manifest_drift_test",
    srcs = ["manifest_drift_test.sh"],
    args = [
        "$(rootpath :gazelle_python_manifest.test)",
        "$(rootpath //bazel/requirements:all.txt)",
        "$(rootpath :gazelle_python.yaml)",
        "$(rootpath @rules_python_gazelle_plugin//manifest/generate:generate_lib_sources_hash)",
    ],
    data = [
        ":gazelle_python.yaml",
        ":gazelle_python_manifest.test",
        "//bazel/requirements:all.txt",
        "@rules_python_gazelle_plugin//manifest/generate:generate_lib_sources_hash",
    ],
    tags = ["manual"],
)
