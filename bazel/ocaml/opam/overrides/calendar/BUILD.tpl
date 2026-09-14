# Override BUILD for calendar 3.0.0 (installed by extension.bzl).
#
# Upstream generates version.ml with a dune rule that expands
# %{version:calendar}. The locked version is already explicit in lock.json, so
# this override materializes the same module without requiring dune's package
# metadata expansion. The remaining library is a direct source glob.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "version_ml",
    outs = ["version.ml"],
    cmd = "echo 'let version = String.trim \"3.0.0\"' > $@",
)

ocaml_library(
    name = "calendarLib",
    srcs = glob([
        "src/*.ml",
        "src/*.mli",
    ]) + [":version.ml"],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_re//:re"],
)
