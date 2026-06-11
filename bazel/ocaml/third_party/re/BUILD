# `re` (ocaml-re) 1.13.2 -- a real external opam dependency, built from source by
# our own ocaml_library via its dune metadata (not vendored, not hand-written).
# The repository rule (//bazel/ocaml/opam:extension.bzl) fetches the pinned
# tarball and generates @ocaml_re's BUILD from the package's `lib/dune`. This
# alias gives it a stable in-tree label alongside the vendored fmt.
#
# re builds wrapped (dune's default): its internal Fmt/Str modules compile as
# Re__Fmt/Re__Str, so linking re next to the vendored fmt no longer collides
# (examples/regex links both deliberately).
alias(
    name = "re",
    actual = "@ocaml_re//:re",
    visibility = ["//visibility:public"],
)
