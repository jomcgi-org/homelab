# gazelle:ignore
# gazelle:exclude extract_debs.py
#
# extract_debs.py is run by the @ocaml_sysroot repository rule via the host
# python3 (ctx.execute), not as a Bazel py_binary. Stop gazelle's python plugin
# from generating a target for it (which would be a broken _py_venv_binary with
# no `main` and break `bazel query //...`).

load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Extractor used by the @ocaml_sysroot repository rule. Exported (not a py_binary)
# because it runs via the host python3 during repository fetch.
exports_files(["extract_debs.py"])

bzl_library(
    name = "repositories",
    srcs = [
        "debs.bzl",
        "repositories.bzl",
    ],
    visibility = ["//visibility:public"],
)
