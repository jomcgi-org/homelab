load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_ppx", "ocaml_test")

# The ppx driver model end to end: ocaml_ppx links a ppxlib standalone driver
# from rewriter libraries (rewriters register themselves at module-init); the
# `preprocess` attr runs it over every source before compilation; the
# rewriter's generated code links against `preprocess_runtime_deps`.
ocaml_ppx(
    name = "show_driver",
    deps = ["@ocaml_ppx_deriving//:ppx_deriving_show"],
)

ocaml_library(
    name = "point",
    srcs = ["point.ml"],
    preprocess = ":show_driver",
    preprocess_runtime_deps = ["@ocaml_ppx_deriving//:ppx_deriving_runtime"],
)

ocaml_test(
    name = "ppx_test",
    srcs = ["ppx_test.ml"],
    deps = [":point"],
)
