load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/ocaml:toolchain.bzl", "declare_ocaml_toolchains")

# Toolchain type consumed by ocaml_library / ocaml_binary.
toolchain_type(
    name = "toolchain_type",
    visibility = ["//visibility:public"],
)

# Per-arch toolchains (ADR 006): ocaml_toolchain_linux_x86_64 / _linux_aarch64,
# generated from the OCAML_ARCHES registry. Each pins exec and target to its
# arch and stages that arch's sysroot. Registered in MODULE.bazel. There is
# deliberately no unconstrained fallback toolchain: one would match any target
# platform from the first (amd64) execution platform, which outranks every
# later platform in resolution, so it would shadow the per-arch toolchains and
# hand aarch64 targets x86_64 binaries.
declare_ocaml_toolchains()

bzl_library(
    name = "defs",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:public"],
    deps = [":rules"],
)

bzl_library(
    name = "rules",
    srcs = ["rules.bzl"],
    visibility = ["//visibility:public"],
    deps = [":toolchain"],
)

bzl_library(
    name = "toolchain",
    srcs = ["toolchain.bzl"],
    visibility = ["//visibility:public"],
    deps = ["//bazel/ocaml/toolchain:arches"],
)
