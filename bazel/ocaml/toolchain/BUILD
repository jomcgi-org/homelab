load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load(":compiler.bzl", "declare_ocaml_sysroots", "ocaml_compiler")

# Builds the pinned Semgrep OCaml fork from source as an RBE action (so it links
# the executor's glibc). Output is a sysroot tar the toolchain stages + extracts.
# This unconstrained target is NOT wired to any toolchain (only the per-arch
# pairs below are registered); it exists for override genrules that stage the
# tar directly (yojson, ocaml-compiler-libs). Both this build action and those
# genrules run on the default executor pool, so the tar's binaries always match
# the executor running them, in every target configuration.
ocaml_compiler(
    name = "ocaml_compiler",
    srcs = "@ocaml_source//:srcs",
    configure = "@ocaml_source//:configure",
    visibility = ["//visibility:public"],
)

# Per-arch sysroots (ADR 006): one exec-constrained ocaml_compiler per enabled
# OCAML_ARCHES entry, so each arch's compiler builds on that arch's executor
# pool. The arm64 sysroot is the same action landing on the arm64 pool.
declare_ocaml_sysroots()

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = [":source"],
)

bzl_library(
    name = "compiler",
    srcs = ["compiler.bzl"],
    visibility = ["//visibility:public"],
    deps = [":arches"],
)

bzl_library(
    name = "arches",
    srcs = ["arches.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "source",
    srcs = ["source.bzl"],
    visibility = ["//visibility:public"],
)
