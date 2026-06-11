load("@rules_cc//cc:defs.bzl", "cc_library")
load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_test")

# A third-party C library, built by cc_library on the RBE executor. `includes`
# exports its directory so consumers' C stubs can #include "adder.h".
cc_library(
    name = "adder_clib",
    srcs = ["adder.c"],
    hdrs = ["adder.h"],
    includes = ["."],
)

# An OCaml library whose C stub binds the cc_library via cc_deps: the header
# dir reaches the stub compile, and the static archive links into any binary.
ocaml_library(
    name = "adder",
    srcs = ["adder.ml"],
    c_srcs = ["adder_stubs.c"],
    cc_deps = [":adder_clib"],
)

ocaml_test(
    name = "adder_test",
    srcs = ["adder_test.ml"],
    deps = [":adder"],
)
