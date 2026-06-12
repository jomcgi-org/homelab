# Override BUILD for tree-sitter-lang.bash (installed by extension.bzl).
#
# Not an opam release: semgrep-bash pinned to the commit Semgrep's submodule
# references (lock version = short sha). The grammar's generated parser.c and
# the C++ scanner build as a cc_library against the vendored tree-sitter
# runtime (includes = ["lib"] resolves the repo-local tree_sitter/parser.h
# copy, kept deliberately by upstream); the OCaml binding stub (bindings.c,
# includes only tree_sitter/api.h) rides c_srcs with the archives via
# cc_deps. cc_library means x86_64-only in CI (the established no-arm64
# pattern); consumers tag accordingly.
load("@rules_cc//cc:defs.bzl", "cc_library")
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

cc_library(
    name = "bash_grammar",
    srcs = [
        "lib/parser.c",
        "lib/scanner.cc",
    ] + glob(["lib/tree_sitter/*.h"]),
    copts = ["-w"],
    includes = ["lib"],
    # The C++ scanner needs the C++ runtime at the final OCaml link
    # (upstream's c_library_flags say -lstdc++); user link flags propagate
    # through cc_deps to every consuming binary.
    linkopts = ["-lstdc++"],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_tree_sitter_c//:tree_sitter"],
)

ocaml_library(
    name = "tree_sitter_bash",
    srcs = [
        "lib/Boilerplate.ml",
        "lib/CST.ml",
        "lib/Parse.ml",
        "lib/Parse.mli",
    ],
    c_srcs = ["lib/bindings.c"],
    cc_deps = [
        ":bash_grammar",
        "@ocaml_tree_sitter_c//:tree_sitter",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_tree_sitter_core//:tree_sitter_run"],
)
