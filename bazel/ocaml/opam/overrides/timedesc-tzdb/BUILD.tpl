# Override BUILD for timedesc-tzdb (installed by extension.bzl).
# Why an override: a dune virtual library; the default implementation
# (timedesc-tzdb.full, the embedded compressed tz database) is what timedesc
# selects, so the virtual interface and the full implementation compile as
# one library.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

# Upstream (rule) copies the embedded database in as module Tzdb_compressed.
genrule(
    name = "tzdb_compressed_ml",
    srcs = ["gen-artifacts/tzdb_compressed_full.ml"],
    outs = ["tzdb_compressed.ml"],
    cmd = "cp $(location gen-artifacts/tzdb_compressed_full.ml) $@",
)

ocaml_library(
    name = "timedesc_tzdb",
    srcs = [
        "timedesc-tzdb/full/timedesc_tzdb.ml",
        "timedesc-tzdb/timedesc_tzdb.mli",
        ":tzdb_compressed_ml",
    ],
    visibility = ["//visibility:public"],
)
