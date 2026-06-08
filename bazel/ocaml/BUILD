load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/ocaml:toolchain.bzl", "ocaml_toolchain")

# Toolchain type consumed by ocaml_library / ocaml_binary.
toolchain_type(
    name = "toolchain_type",
    visibility = ["//visibility:public"],
)

# Tool configuration (opam root, ocamlfind preference, extra flags). The actual
# compiler is supplied by the pinned container image (see toolchain.bzl), not by
# this target.
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
)
