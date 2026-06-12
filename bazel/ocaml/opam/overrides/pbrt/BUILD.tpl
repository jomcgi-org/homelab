# Override BUILD for pbrt (installed by extension.bzl).
# Why an override: the ocaml-protoc monorepo's runtime dune uses
# (foreign_stubs) for its varint C accelerator and bumps -inline, neither of
# which dune2bazel models. One library, one C stub.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "pbrt",
    srcs = [
        "src/runtime/pbrt.ml",
        "src/runtime/pbrt.mli",
    ],
    c_srcs = ["src/runtime/stubs.c"],
    # Upstream: "we need to increase -inline, so that the varint
    # encoder/decoder can be remembered by the inliner."
    ocamlopt_flags = [
        "-inline",
        "100",
    ],
    visibility = ["//visibility:public"],
)
