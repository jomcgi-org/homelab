load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/ocaml:toolchain.bzl", "declare_ocaml_toolchains", "ocaml_toolchain")

# Toolchain type consumed by ocaml_library / ocaml_binary.
toolchain_type(
    name = "toolchain_type",
    visibility = ["//visibility:public"],
)

# Per-arch toolchains (ADR 006): ocaml_toolchain_linux_x86_64 / _linux_aarch64,
# generated from the OCAML_ARCHES registry. Each pins exec and target to its
# arch and stages that arch's sysroot. Registered (constrained first) in
# MODULE.bazel.
declare_ocaml_toolchains()

# Unconstrained fallback, registered last: preserves the original single-arch
# behavior (default pool, x86_64 sysroot) for any platform the registry does
# not model, e.g. a darwin host build outside CI.
ocaml_toolchain(
    name = "ocaml_tools",
    visibility = ["//visibility:public"],
)

toolchain(
    name = "ocaml_toolchain",
    toolchain = ":ocaml_tools",
    toolchain_type = ":toolchain_type",
    visibility = ["//visibility:public"],
)

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
