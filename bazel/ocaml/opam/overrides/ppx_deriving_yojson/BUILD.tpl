# Override BUILD for ppx_deriving_yojson (installed by extension.bzl).
# Why an override: the dune file splits one src/ dir into two libraries with
# (modules ...) filters, which the translator does not model. The deriver is
# metaquot-preprocessed (the in-repo ppxlib driver).
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "ppx_deriving_yojson_runtime",
    srcs = [
        "src/ppx_deriving_yojson_runtime.ml",
        "src/ppx_deriving_yojson_runtime.mli",
    ],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_ppx_deriving//:ppx_deriving_runtime"],
)

ocaml_library(
    name = "ppx_deriving_yojson",
    srcs = [
        "src/ppx_deriving_yojson.ml",
        "src/ppx_deriving_yojson.mli",
    ],
    ocamlopt_flags = [
        "-w",
        "-9",
    ],
    preprocess = "@ocaml_ppxlib//:metaquot_driver",
    visibility = ["//visibility:public"],
    deps = [
        "@ocaml_ppx_deriving//:ppx_deriving_api",
        "@ocaml_ppxlib//:ppxlib",
    ],
    opam_deps = ["compiler-libs.common"],
)
