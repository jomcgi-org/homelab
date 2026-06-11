# Override BUILD for ppx_deriving (installed by extension.bzl).
#
# Why an override: the api/runtime sources are cppo templates expanded with the
# compiler version (the driver's `cppo` attr models that), and the api + the
# plugins are themselves preprocessed with ppxlib.metaquot -- a ppx driver run
# at build time (the `preprocess` attr + an ocaml_ppx target).
#
# Only the show plugin is built today (the 159-use case in Semgrep CE per ADR
# 004); the other src_plugins follow the same shape when needed.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_ppx")

# metaquot driver used to preprocess the api and the plugins.
ocaml_ppx(
    name = "metaquot_ppx",
    deps = ["@ocaml_ppxlib//:ppxlib_metaquot"],
)

ocaml_library(
    name = "ppx_deriving_runtime",
    srcs = glob(["src/runtime/*.cppo.ml", "src/runtime/*.cppo.mli"], allow_empty = True),
    cppo = "@ocaml_cppo//:cppo",
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ppx_deriving_api",
    srcs = glob(["src/api/*.cppo.ml", "src/api/*.cppo.mli"], allow_empty = True),
    cppo = "@ocaml_cppo//:cppo",
    preprocess = ":metaquot_ppx",
    deps = [
        "@ocaml_ppx_derivers//:ppx_derivers",
        "@ocaml_ppxlib//:ppxlib",
    ],
    opam_deps = ["compiler-libs.common"],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ppx_deriving_show",
    srcs = glob(["src_plugins/show/*.ml", "src_plugins/show/*.mli"], allow_empty = True),
    preprocess = ":metaquot_ppx",
    deps = [
        ":ppx_deriving_api",
        "@ocaml_ppxlib//:ppxlib",
    ],
    opam_deps = ["compiler-libs.common"],
    visibility = ["//visibility:public"],
)
