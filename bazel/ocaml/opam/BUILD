# gazelle:ignore
# gazelle:exclude dune2bazel.py
#
# dune2bazel.py is run by the @ocaml_* repository rules (extension.bzl) via the
# host python3 (ctx.execute), not as a Bazel py_binary. Stop gazelle's python
# plugin from generating a target for it (a broken _py_venv_binary with no
# `main`, which would break `bazel query //...`).

load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Generator used by the @ocaml_* repository rules. Exported (not a py_binary)
# because it runs via the host python3 during repository fetch.
exports_files(["dune2bazel.py"])

bzl_library(
    name = "extension",
    srcs = [
        "extension.bzl",
        "packages.bzl",
    ],
    visibility = ["//visibility:public"],
)
