# Override BUILD for the tree-sitter C runtime (installed by extension.bzl).
#
# Not an opam package: the upstream tree-sitter runtime, vendored as a
# cc_library for OCaml bindings to link through cc_deps. lib/src/lib.c is the
# official amalgamation (it #includes every runtime .c), so the library is one
# compilation unit; the public API is lib/include/tree_sitter/api.h.
load("@rules_cc//cc:defs.bzl", "cc_library")

cc_library(
    name = "tree_sitter",
    srcs = ["lib/src/lib.c"] + glob([
        "lib/src/*.h",
        "lib/src/unicode/*.h",
    ]),
    hdrs = glob(["lib/include/tree_sitter/*.h"]),
    copts = ["-w"],
    includes = [
        "lib/include",
        "lib/src",
    ],
    textual_hdrs = glob(
        ["lib/src/*.c"],
        exclude = ["lib/src/lib.c"],
    ),
    visibility = ["//visibility:public"],
)
