# Override BUILD for atd (installed by extension.bzl).
#
# Why an override: the ahrefs/atd tarball is a monorepo of many packages
# (atd, atdgen, atdgen-runtime, atdcpp, atdpy, ...). We build only the three we
# need -- the atd library, the atdgen runtime, and the atdgen code generator --
# from their own dune metadata (ocamllex + menhir, both supported by our rules).
# The atdgen binary is an executable stanza, which the translator does not emit,
# so it lives here too.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library")

# The ATD surface library: lexers (ocamllex) + grammar (menhir, with inference).
ocaml_library(
    name = "atd",
    srcs = glob([
        "atd/src/*.ml",
        "atd/src/*.mli",
        "atd/src/*.mll",
        "atd/src/*.mly",
    ]),
    menhir = ["parser"],
    menhir_tool = "@ocaml_menhir//:menhir",
    opam_deps = ["unix"],
    deps = [
        "@ocaml_easy_format//:easy_format",
        "@ocaml_re//:re",
        "@ocaml_yojson//:yojson",
    ],
    visibility = ["//visibility:public"],
)

# Runtime support linked by atdgen-generated code.
ocaml_library(
    name = "atdgen_runtime",
    srcs = glob([
        "atdgen-runtime/src/*.ml",
        "atdgen-runtime/src/*.mli",
    ]),
    deps = [
        "@ocaml_biniou//:biniou",
        "@ocaml_yojson//:yojson",
    ],
    visibility = ["//visibility:public"],
)

# The code-emitter library behind the atdgen binary.
ocaml_library(
    name = "atdgen_emit",
    srcs = glob([
        "atdgen/src/*.ml",
        "atdgen/src/*.mli",
        "atdgen/src/*.mll",
    ]),
    deps = [
        ":atd",
        "@ocaml_biniou//:biniou",
        "@ocaml_easy_format//:easy_format",
        "@ocaml_re//:re",
        "@ocaml_yojson//:yojson",
    ],
    visibility = ["//visibility:public"],
)

# The atdgen code generator: consumed as a build tool by the `atd` codegen
# (an @ocaml_atd//:atdgen genrule in a service's BUILD).
ocaml_binary(
    name = "atdgen",
    srcs = ["atdgen/bin/ag_main.ml"],
    opam_deps = ["unix"],
    deps = [
        ":atd",
        ":atdgen_emit",
        "@ocaml_re//:re",
    ],
    visibility = ["//visibility:public"],
)
