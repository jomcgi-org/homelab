# Override BUILD for ANSITerminal (installed by extension.bzl).
# Why an override: the dune tree selects the unix or windows implementation by
# running choose_implementation.exe (a (rule) producing ANSITerminal.ml and
# ANSITerminal_stubs.c). The executors are linux only, so the choice is pinned:
# copy the unix variants.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "impl_ml",
    srcs = ["src/ANSITerminal_unix.ml"],
    outs = ["ANSITerminal.ml"],
    cmd = "cp $(location src/ANSITerminal_unix.ml) $@",
)

genrule(
    name = "impl_stubs",
    srcs = ["src/ANSITerminal_unix_stubs.c"],
    outs = ["ANSITerminal_stubs.c"],
    cmd = "cp $(location src/ANSITerminal_unix_stubs.c) $@",
)

ocaml_library(
    name = "ANSITerminal",
    srcs = [
        "src/ANSITerminal.mli",
        "src/ANSITerminal_common.ml",
        ":impl_ml",
    ],
    c_srcs = [":impl_stubs"],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
)
