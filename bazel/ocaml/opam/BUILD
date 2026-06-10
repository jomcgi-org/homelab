# gazelle:ignore
# gazelle:exclude dune2bazel.py
# gazelle:exclude dune2bazel_test.py
#
# dune2bazel.py is run by the @ocaml_* repository rules (extension.bzl) via the
# host python3 (ctx.execute), not as a Bazel py_binary. Stop gazelle's python
# plugin from generating a target for it (a broken _py_venv_binary with no
# `main`, which would break `bazel query //...`).

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Generator used by the @ocaml_* repository rules. Exported (not a py_binary)
# because it runs via the host python3 during repository fetch.
exports_files(["dune2bazel.py"])

py_library(
    name = "dune2bazel_lib",
    srcs = ["dune2bazel.py"],
    visibility = ["//:__subpackages__"],
)

py_test(
    name = "dune2bazel_test",
    srcs = ["dune2bazel_test.py"],
    deps = [
        ":dune2bazel_lib",
        "@pip//pytest",
    ],
)

bzl_library(
    name = "extension",
    srcs = [
        "extension.bzl",
        "packages.bzl",
    ],
    visibility = ["//visibility:public"],
)
