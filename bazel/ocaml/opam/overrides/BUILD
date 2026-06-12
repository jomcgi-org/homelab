# Hand-written BUILD overrides for opam packages whose dune trees need codegen
# or compiler introspection dune2bazel.py does not model (lock.json entries
# with "override": true). Each BUILD.tpl is installed verbatim as the fetched
# repo's BUILD.bazel by bazel/ocaml/opam/extension.bzl.
#
# Every override is a standing TODO: it documents exactly which dune features
# the translator would need to learn to retire it.
exports_files(glob([
    "*/BUILD.tpl",
    "*/BUILD.extra.tpl",
]))
