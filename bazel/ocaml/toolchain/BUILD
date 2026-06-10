load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load(":compiler.bzl", "ocaml_compiler")

# Builds the pinned Semgrep OCaml fork from source as an RBE action (so it links
# the executor's glibc). Output is a sysroot tar the toolchain stages + extracts.
ocaml_compiler(
    name = "ocaml_compiler",
    srcs = "@ocaml_source//:srcs",
    configure = "@ocaml_source//:configure",
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = [":source"],
)

bzl_library(
    name = "arches",
    srcs = ["arches.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "compiler",
    srcs = ["compiler.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "source",
    srcs = ["source.bzl"],
    visibility = ["//visibility:public"],
)
