load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

# Override BUILD for tree-sitter-lang.bash (installed by extension.bzl).
#
# Not an opam release: semgrep-bash pinned to the commit Semgrep's submodule
# references (lock version = short sha). The grammar's generated parser.c,
# C++ scanner, and OCaml binding stub all ride c_srcs so the pinned native
# tool closure compiles them. The header-only cc_library preserves the
# tree_sitter/parser.h include layout and propagates the declared libc++ link
# input. CI compiles the library on both x86_64 and aarch64.
load("@rules_cc//cc:defs.bzl", "cc_library")

cc_library(
    name = "bash_headers",
    hdrs = glob(["lib/tree_sitter/*.h"]),
    includes = ["lib"],
    # The C++ scanner is compiled with Zig libc++, so the final OCaml link must
    # use the same pinned C++ runtime.
    linkopts = ["-lc++"],
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
    c_srcs = [
        "lib/bindings.c",
        "lib/parser.c",
        "lib/scanner.cc",
    ],
    cc_deps = [
        ":bash_headers",
        "@ocaml_tree_sitter_c//:tree_sitter",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_tree_sitter_core//:tree_sitter_run"],
)
