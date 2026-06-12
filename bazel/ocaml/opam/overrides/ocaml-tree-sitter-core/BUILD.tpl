# Override BUILD for ocaml-tree-sitter-core (installed by extension.bzl).
#
# Not an opam release: the returntocorp repo pinned to the commit Semgrep's
# submodule references (lock version = short sha). Three libraries:
#
#   * tree-sitter.bindings: the OCaml binding to the tree-sitter C API. The
#     dune tree probes TREESITTER_INCDIR/LIBDIR with a configurator; the
#     vendored runtime cc_library replaces that through cc_deps. The
#     Tree_sitter_output atd codegen runs the from-source atdgen (the
#     examples/atdgen pattern).
#   * tree-sitter.gen: the CST generator support library (pps show/ord).
#   * tree-sitter.run: the runtime the per-language parsers link.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_ppx")

_ATDGEN = "@ocaml_atd//:atdgen"

[
    genrule(
        name = "atd_%s" % base,
        srcs = ["%s/%s.atd" % (d, base)],
        outs = [
            "%s_t.ml" % base,
            "%s_t.mli" % base,
            "%s_j.ml" % base,
            "%s_j.mli" % base,
        ],
        cmd = "W=$$(mktemp -d) && AG=$$(realpath $(location %s)) && " % _ATDGEN +
              ("cp $(location {d}/{b}.atd) $$W/{b}.atd && " +
               "(cd $$W && $$AG -t {b}.atd && $$AG -j -j-std {b}.atd) && " +
               "cp $$W/{b}_t.ml $(location {b}_t.ml) && " +
               "cp $$W/{b}_t.mli $(location {b}_t.mli) && " +
               "cp $$W/{b}_j.ml $(location {b}_j.ml) && " +
               "cp $$W/{b}_j.mli $(location {b}_j.mli)").format(b = base, d = d),
        tools = [_ATDGEN],
    )
    for base, d in [
        ("Tree_sitter_output", "src/bindings/lib"),
        ("Tree_sitter", "src/gen/lib"),
        ("Tree_sitter_error", "src/run/lib"),
    ]
]

ocaml_library(
    name = "tree_sitter_bindings",
    srcs = glob(
        [
            "src/bindings/lib/*.ml",
            "src/bindings/lib/*.mli",
        ],
        allow_empty = True,
    ) + [
        "Tree_sitter_output_j.ml",
        "Tree_sitter_output_j.mli",
        "Tree_sitter_output_t.ml",
        "Tree_sitter_output_t.mli",
    ],
    c_srcs = ["src/bindings/lib/bindings.c"],
    cc_deps = ["@ocaml_tree_sitter_c//:tree_sitter"],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_atd//:atdgen_runtime"],
)

ocaml_ppx(
    name = "show_ord_ppx",
    deps = [
        "@ocaml_ppx_deriving//:ppx_deriving_ord",
        "@ocaml_ppx_deriving//:ppx_deriving_show",
    ],
)

ocaml_library(
    name = "tree_sitter_gen",
    srcs = glob(
        [
            "src/gen/lib/*.ml",
            "src/gen/lib/*.mli",
        ],
        allow_empty = True,
    ) + [
        "Tree_sitter_j.ml",
        "Tree_sitter_j.mli",
        "Tree_sitter_t.ml",
        "Tree_sitter_t.mli",
    ],
    opam_deps = [
        "unix",
        "str",
    ],
    preprocess = ":show_ord_ppx",
    preprocess_runtime_deps = ["@ocaml_ppx_deriving//:ppx_deriving_runtime"],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        "@ocaml_atd//:atdgen_runtime",
        "@ocaml_re//:re",
        "@ocaml_tsort//:tsort",
    ],
)

ocaml_library(
    name = "tree_sitter_run",
    srcs = glob(
        [
            "src/run/lib/*.ml",
            "src/run/lib/*.mli",
        ],
        allow_empty = True,
    ) + [
        "Tree_sitter_error_j.ml",
        "Tree_sitter_error_j.mli",
        "Tree_sitter_error_t.ml",
        "Tree_sitter_error_t.mli",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":tree_sitter_bindings",
        ":tree_sitter_gen",
        "@ocaml_ANSITerminal//:ANSITerminal",
        "@ocaml_atd//:atdgen_runtime",
        "@ocaml_cmdliner//:cmdliner",
        "@ocaml_sexplib//:sexplib",
    ],
)
