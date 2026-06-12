# Override BUILD for ppxlib (installed by extension.bzl).
#
# Why an override: ppxlib bootstraps itself with in-tree codegen --
#   * astlib sources are filtered per compiler version by pp/pp.exe
#     (the (*IF_CURRENT/IF_AT_LEAST*) markers select the AST that aliases the
#     running compiler's Parsetree); the version token comes from the driver's
#     %OCAML_AST_VERSION% substitution, so it tracks the sysroot compiler,
#   * src/ast_pattern_generated.ml + ast_builder_generated.ml are produced by
#     gen/ executables that read ppxlib.ast's own ast.ml,
#   * src/skip_hash_bang.mll goes through ocamllex.
# The cinaps stanzas are dev-only consistency checks (their output is already
# committed upstream) and are intentionally dropped.
#
# Sub-libraries not (yet) built: bin/ and runner/ executables.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library", "ocaml_ppx")

# --- bootstrap: the per-version source preprocessors --------------------------
# Two distinct tools named pp.exe upstream: astlib/pp keys on the short AST
# token (503) and the ast_<token>.ml file name; ast/pp takes the dotted
# version, rewrites the literal OCAML_VERSION, and handles IF_AT_LEAST.

ocaml_binary(
    name = "astlib_pp",
    srcs = [
        "astlib/pp/pp.ml",
        "astlib/pp/pp_rewrite.mll",
    ],
)

ocaml_library(
    name = "supported_version",
    srcs = glob(["ast/supported_version/*.ml", "ast/supported_version/*.mli"], allow_empty = True),
    wrapped = True,
)

ocaml_binary(
    name = "ast_pp",
    srcs = [
        "ast/pp/pp.ml",
        "ast/pp/pp.mli",
        "ast/pp/pp_rewrite.mli",
        "ast/pp/pp_rewrite.mll",
    ],
    ocamlopt_flags = [
        "-w",
        "-3",
    ],
    deps = [":supported_version"],
)

# --- the AST ladder -----------------------------------------------------------

ocaml_library(
    name = "astlib",
    srcs = glob(["astlib/*.ml", "astlib/*.mli"], allow_empty = True),
    ocamlopt_flags = [
        "-w",
        "-9",
    ],
    pp = ":astlib_pp",
    pp_args = ["%OCAML_AST_VERSION%"],
    wrapped = True,
    deps = ["@ocaml_compiler_libs_pkg//:ocaml_common"],
    opam_deps = ["compiler-libs.common"],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ppxlib_ast",
    srcs = glob(["ast/*.ml", "ast/*.mli"], allow_empty = True),
    ocamlopt_flags = [
        "-safe-string",
        "-w",
        "-9-27-32",
    ],
    pp = ":ast_pp",
    pp_args = ["%OCAML_VERSION%"],
    wrapped = True,
    deps = [
        ":astlib",
        "@ocaml_stdlib_shims//:stdlib_shims",
    ],
    visibility = ["//visibility:public"],
)

# --- plain support libraries --------------------------------------------------

ocaml_library(
    name = "stdppx",
    srcs = glob(["stdppx/*.ml", "stdppx/*.mli"], allow_empty = True),
    ocamlopt_flags = ["-safe-string"],
    wrapped = True,
    deps = [
        "@ocaml_sexplib0//:sexplib0",
        "@ocaml_stdlib_shims//:stdlib_shims",
    ],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ppxlib_traverse_builtins",
    srcs = glob(["traverse_builtins/*.ml", "traverse_builtins/*.mli"], allow_empty = True),
    ocamlopt_flags = ["-safe-string"],
    wrapped = True,
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ppxlib_print_diff",
    srcs = glob(["print-diff/*.ml", "print-diff/*.mli"], allow_empty = True),
    ocamlopt_flags = ["-safe-string"],
    wrapped = True,
    visibility = ["//visibility:public"],
)

# --- src codegen: combinators generated from the AST itself -------------------

ocaml_binary(
    name = "gen_ast_pattern",
    srcs = [
        "src/gen/gen_ast_pattern.ml",
        "src/gen/gen_ast_pattern.mli",
        "src/gen/import.ml",
    ],
    deps = [
        ":astlib",
        ":ppxlib_ast",
        ":ppxlib_traverse_builtins",
        ":stdppx",
        "@ocaml_stdlib_shims//:stdlib_shims",
    ],
)

ocaml_binary(
    name = "gen_ast_builder",
    srcs = [
        "src/gen/gen_ast_builder.ml",
        "src/gen/gen_ast_builder.mli",
        "src/gen/import.ml",
    ],
    deps = [
        ":astlib",
        ":ppxlib_ast",
        ":ppxlib_traverse_builtins",
        ":stdppx",
        "@ocaml_stdlib_shims//:stdlib_shims",
    ],
)

genrule(
    name = "ast_pattern_generated",
    srcs = ["ast/ast.ml"],
    outs = ["src/ast_pattern_generated.ml"],
    cmd = "$(location :gen_ast_pattern) $(location ast/ast.ml) && mv ast_pattern_generated.ml $@",
    tools = [":gen_ast_pattern"],
)

genrule(
    name = "ast_builder_generated",
    srcs = ["ast/ast.ml"],
    outs = ["src/ast_builder_generated.ml"],
    cmd = "$(location :gen_ast_builder) $(location ast/ast.ml) && mv ast_builder_generated.ml $@",
    tools = [":gen_ast_builder"],
)

# --- ppxlib itself -------------------------------------------------------------

ocaml_library(
    name = "ppxlib",
    srcs = glob(["src/*.ml", "src/*.mli", "src/*.mll"], allow_empty = True) + [
        ":ast_builder_generated",
        ":ast_pattern_generated",
    ],
    ocamlopt_flags = ["-safe-string"],
    wrapped = True,
    deps = [
        ":astlib",
        ":ppxlib_ast",
        ":ppxlib_print_diff",
        ":ppxlib_traverse_builtins",
        ":stdppx",
        "@ocaml_compiler_libs_pkg//:ocaml_shadow",
        "@ocaml_ppx_derivers//:ppx_derivers",
        "@ocaml_sexplib0//:sexplib0",
        "@ocaml_stdlib_shims//:stdlib_shims",
    ],
    opam_deps = ["compiler-libs.common"],
    visibility = ["//visibility:public"],
)

# --- metaquot: the [%expr ...] quotation rewriter ------------------------------

ocaml_library(
    name = "ppxlib_metaquot_lifters",
    srcs = glob(["metaquot_lifters/*.ml", "metaquot_lifters/*.mli"], allow_empty = True),
    ocamlopt_flags = ["-safe-string"],
    wrapped = True,
    deps = [
        ":ppxlib",
        ":ppxlib_traverse_builtins",
        ":stdppx",
        "@ocaml_stdlib_shims//:stdlib_shims",
    ],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ppxlib_metaquot",
    srcs = glob(["metaquot/*.ml", "metaquot/*.mli"], allow_empty = True),
    ocamlopt_flags = ["-safe-string"],
    wrapped = True,
    deps = [
        ":astlib",
        ":ppxlib",
        ":ppxlib_metaquot_lifters",
        ":ppxlib_traverse_builtins",
    ],
    visibility = ["//visibility:public"],
)

# --- traverse: the [@@deriving traverse] classes -------------------------------
# Its sources use [%expr ...] quotations, so it is the first in-repo library
# preprocessed by its own sibling metaquot (upstream: (preprocess (pps
# ppxlib_metaquot))). The consumer is ppx_sexp_conv's expander.

ocaml_ppx(
    name = "metaquot_driver",
    deps = [":ppxlib_metaquot"],
)

ocaml_library(
    name = "ppxlib_traverse",
    srcs = glob(
        [
            "traverse/*.ml",
            "traverse/*.mli",
        ],
        allow_empty = True,
    ),
    ocamlopt_flags = ["-safe-string"],
    preprocess = ":metaquot_driver",
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":ppxlib",
        ":ppxlib_ast",
        ":ppxlib_traverse_builtins",
        ":stdppx",
        "@ocaml_stdlib_shims//:stdlib_shims",
    ],
)
