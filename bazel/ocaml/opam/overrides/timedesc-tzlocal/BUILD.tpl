# Override BUILD for timedesc-tzlocal (installed by extension.bzl).
# Why an override: a dune virtual library; the default implementation
# (timedesc-tzlocal.unix-or-utc) copies the unix prober in as a member module
# and falls back to UTC, which is the right behavior on the executors.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "unix_impl_ml",
    srcs = ["timedesc-tzlocal/unix/timedesc_tzlocal.ml"],
    outs = ["unix_timedesc_tzlocal.ml"],
    cmd = "cp $(location timedesc-tzlocal/unix/timedesc_tzlocal.ml) $@",
)

ocaml_library(
    name = "timedesc_tzlocal",
    srcs = [
        "timedesc-tzlocal/timedesc_tzlocal.mli",
        "timedesc-tzlocal/unix-or-utc/timedesc_tzlocal.ml",
        ":unix_impl_ml",
    ],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
)
