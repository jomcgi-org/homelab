load("//bazel/ocaml:defs.bzl", "ocaml_ppx", "ocaml_test")

# ppx_blob + preprocess_data end to end. ocaml_ppx links a ppxlib standalone
# driver from the locked ppx_blob rewriter; the `preprocess` attr runs it over
# the source, where [%blob "secret.txt"] embeds the file's contents as a string
# literal. `preprocess_data` (dune's (preprocessor_deps (file X))) stages
# secret.txt into the work dir so the rewriter finds it at preprocess time.
# The test asserts the embedded string equals the file, so a green run proves
# the blob was read and inlined during the ppx pass.
ocaml_ppx(
    name = "blob_driver",
    deps = ["@ocaml_ppx_blob//:ppx_blob"],
)

ocaml_test(
    name = "blob_test",
    srcs = ["blob_test.ml"],
    preprocess = ":blob_driver",
    preprocess_data = ["secret.txt"],
)
