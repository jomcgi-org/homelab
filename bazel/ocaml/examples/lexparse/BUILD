load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_test")

# Codegen end to end: a calculator whose lexer is ocamllex (.mll) and whose
# parser is menhir (.mly, with OCaml type inference). The driver compiles the
# sibling `Ast` first so menhir's --infer can type the semantic actions, then
# generates and compiles the parser and lexer.
ocaml_library(
    name = "calc",
    srcs = [
        "ast.ml",
        "lexer.mll",
        "parser.mly",
    ],
    menhir = ["parser"],
    menhir_flags = ["--explain"],
    menhir_tool = "@ocaml_menhir//:menhir",
)

ocaml_test(
    name = "lexparse_test",
    srcs = ["lexparse_test.ml"],
    deps = [":calc"],
)
