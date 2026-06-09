# `re` (ocaml-re) 1.11.0 -- a real external opam dependency, built from source by
# our own ocaml_library via its dune metadata (not vendored, not hand-written).
# The repository rule (//bazel/ocaml/opam:extension.bzl) fetches the pinned
# tarball and generates @ocaml_re's BUILD from the package's `lib/dune`. This
# alias gives it a stable in-tree label alongside the vendored fmt.
#
# Note: re ships internal modules named Fmt and Str, so a binary must not link
# both //bazel/ocaml/third_party/re and //bazel/ocaml/third_party/fmt (duplicate
# `Fmt` module) -- see bazel/ocaml/examples/regex, which depends on re alone.
alias(
    name = "re",
    actual = "@ocaml_re//:re",
    visibility = ["//visibility:public"],
)
