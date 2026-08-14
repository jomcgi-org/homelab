"""Targets in the repository root"""

# We prefer BUILD instead of BUILD.bazel
# gazelle:build_file_name BUILD
load("@gazelle//:def.bzl", "gazelle", "gazelle_binary")
load("@npm//:defs.bzl", "npm_link_all_packages")
# Python gazelle config moved to //bazel/tools/python to avoid eager-fetching all pip packages during CI analysis

npm_link_all_packages(name = "node_modules")

# gazelle:prefix github.com/jomcgi/homelab
# How gazelle labels deps that live in EXTERNAL Go repos. Under bzlmod the
# go_deps extension generates targets with the "import" convention (the target
# is named for the Go package, so the label is @repo//pkg/authn, and
# @repo//pkg/v1:pkg where the package name differs from the directory). Gazelle
# still defaults this to the legacy "go_default_library", so without this
# directive every `ci regen` rewrites all external Go deps to
# @repo//pkg/authn:go_default_library. Those targets do not exist, and the tree
# then fails ANALYSIS rather than anything obvious: `//bazel/semgrep/defs/gazelle
# :gazelle_test` aborts the build before a single test runs. The repo's own Go
# targets already match the default, so only the external convention is set.
# gazelle:go_naming_convention_external import
# bazel_gazelle is brought in under the repo name `gazelle`, and its targets use
# the import convention. Gazelle's resolver does not know either fact about
# itself, so without these it rewrites this repo's two gazelle plugins to
# @bazel_gazelle//config:go_default_library and friends, which do not exist.
# gazelle:resolve go github.com/bazelbuild/bazel-gazelle/config @gazelle//config
# gazelle:resolve go github.com/bazelbuild/bazel-gazelle/label @gazelle//label
# gazelle:resolve go github.com/bazelbuild/bazel-gazelle/language @gazelle//language
# gazelle:resolve go github.com/bazelbuild/bazel-gazelle/repo @gazelle//repo
# gazelle:resolve go github.com/bazelbuild/bazel-gazelle/resolve @gazelle//resolve
# gazelle:resolve go github.com/bazelbuild/bazel-gazelle/rule @gazelle//rule
# gazelle:exclude .claude
# Explicit-only agent procedures + helper scripts (not py packages; imports are
# runtime/monolith paths Gazelle cannot resolve). Same class as .claude/skills.
# gazelle:exclude docs/runbooks

# gazelle:semgrep_target_kinds py_venv_binary

# Custom gazelle binary with ArgoCD extensions
gazelle_binary(
    name = "gazelle_binary",
    languages = [
        "//bazel/helm/gazelle",
        "//bazel/semgrep/defs/gazelle",
        "@bazel_skylib_gazelle_plugin//bzl",
        "@gazelle//language/go",
        "@gazelle//language/proto",
        "@rules_python_gazelle_plugin//python",
    ],
)

gazelle(
    name = "gazelle",
    env = {
        "ENABLE_LANGUAGES": ",".join([
            "argocd",
            "semgrep",
            "bzl",
            "proto",
            "go",
            "python",
        ]),
    },
    gazelle = ":gazelle_binary",
    visibility = ["//bazel/tools/format:__pkg__"],
)

exports_files(
    ["pyproject.toml"],
    visibility = ["//:__subpackages__"],
)

exports_files(
    ["buildbuddy.yaml"],
    visibility = ["//bazel/tools/ci:__pkg__"],
)

# Produce aspect_rules_py targets rather than rules_python
# gazelle:map_kind py_binary py_venv_binary @aspect_rules_py//py/private/py_venv:defs.bzl
# gazelle:map_kind py_library py_library @aspect_rules_py//py:defs.bzl
# gazelle:map_kind py_test py_test //bazel/tools/pytest:defs.bzl
#
# Don't walk into virtualenvs when looking for python sources.
# We don't intend to plant BUILD files there.
# gazelle:exclude **/*.venv
#
# gazelle:python_manifest_file_name bazel/tools/python/gazelle_python.yaml
#
# Python gazelle configuration moved to //bazel/tools/python to avoid eager-fetching
# all pip packages during CI analysis phase. Use:
# - bazel run //bazel/tools/python:gazelle_python_manifest.update
# - bazel test //bazel/tools/python:gazelle_python_manifest.test
