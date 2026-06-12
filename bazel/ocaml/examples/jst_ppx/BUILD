load("@homelab//bazel/ocaml:defs.bzl", "ocaml_ppx", "ocaml_test")

# Wave B acceptance (Phase 8): the Jane Street shallow ladder end to end.
# One driver composes the three derivers; the test derives compare/sexp_of/
# hash on a record and exercises all three. Runs on both arches (the arm64
# shard builds //bazel/ocaml/...).
ocaml_ppx(
    name = "jst_derivers",
    deps = [
        "@ocaml_ppx_compare//:ppx_compare",
        "@ocaml_ppx_hash//:ppx_hash",
        "@ocaml_ppx_sexp_conv//:ppx_sexp_conv",
    ],
)

ocaml_test(
    name = "jst_ppx_test",
    srcs = ["derive_test.ml"],
    preprocess = ":jst_derivers",
    preprocess_runtime_deps = [
        "@ocaml_ppx_compare//:ppx_compare_lib",
        "@ocaml_ppx_hash//:ppx_hash_lib",
        "@ocaml_ppx_sexp_conv//:ppx_sexp_conv_lib",
    ],
    deps = [
        "@ocaml_base//:base",
        "@ocaml_sexplib0//:sexplib0",
    ],
)
