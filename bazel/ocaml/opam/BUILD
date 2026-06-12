# gazelle:ignore
# gazelle:exclude dune2bazel.py
# gazelle:exclude dune2bazel_test.py
# gazelle:exclude update_lock.py
# gazelle:exclude test_update_lock.py
#
# dune2bazel.py is run by the @ocaml_* repository rules (extension.bzl) via the
# host python3 (ctx.execute), not as a Bazel py_binary, and update_lock.py is a
# manual workstation tool (network) that maintains lock.json. Stop gazelle's
# python plugin from generating targets for them (a broken _py_venv_binary with
# no `main`, which would break `bazel query //...`).

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Generator used by the @ocaml_* repository rules. Exported (not a py_binary)
# because it runs via the host python3 during repository fetch. lock.json is
# read by the module extension (mctx.read).
exports_files([
    "dune2bazel.py",
    "lock.json",
])

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

py_library(
    name = "update_lock_lib",
    srcs = ["update_lock.py"],
    visibility = ["//:__subpackages__"],
)

py_test(
    name = "test_update_lock",
    srcs = ["test_update_lock.py"],
    deps = [
        ":update_lock_lib",
        "@pip//pytest",
    ],
)

bzl_library(
    name = "extension",
    srcs = ["extension.bzl"],
    visibility = ["//visibility:public"],
)
