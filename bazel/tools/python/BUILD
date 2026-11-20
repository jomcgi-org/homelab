"""Python import mapping for Gazelle and its lock drift test."""

load("@pip//:requirements.bzl", "all_whl_requirements_by_package")
load("@rules_python_gazelle_plugin//manifest:defs.bzl", "gazelle_python_manifest")
load("@rules_python_gazelle_plugin//modules_mapping:def.bzl", "modules_mapping")

_PLATFORM_WHEELS = {
    "colorama": "@manifest_colorama//file",
    "httpx2_jsfetch": "@manifest_httpx2_jsfetch//file",
    "pywin32": "@manifest_pywin32//file",
    "pywin32_ctypes": "@manifest_pywin32_ctypes//file",
}

# Fetches metadata for python packages we depend on.
modules_mapping(
    name = "modules_map",
    wheels = [wheel for name, wheel in all_whl_requirements_by_package.items() if name not in _PLATFORM_WHEELS] + _PLATFORM_WHEELS.values(),
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
