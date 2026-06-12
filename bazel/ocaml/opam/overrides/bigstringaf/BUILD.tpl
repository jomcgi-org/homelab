# Override BUILD for bigstringaf (installed by extension.bzl).
#
# Why an override: the dune library carries (foreign_stubs ...) whose flags
# come from a (rule) running a dune-configurator probe (config/discover.ml).
# That probe is provably droppable here: it writes only C *warning* flags
# (-Wall -Wextra -Wpedantic, or /Wall /W3 for msvc) to cflags.sexp, nothing
# that changes codegen or linkage, so the stub compiles exactly the same
# without it. The (js_of_ocaml ...) field is likewise inert for a native
# build. With both gone the library is one ocaml_library with a C stub.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "bigstringaf",
    srcs = [
        "lib/bigstringaf.ml",
        "lib/bigstringaf.mli",
    ],
    c_srcs = ["lib/bigstringaf_stubs.c"],
    visibility = ["//visibility:public"],
    wrapped = True,
)
