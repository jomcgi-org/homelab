# Override BUILD for cmdliner (installed by bazel/ocaml/opam/extension.bzl).
#
# Why an override: cmdliner is not a dune project (it builds with b0/topkg
# Makefiles), so dune2bazel has nothing to translate. The library itself is a
# flat set of Cmdliner_* modules plus the Cmdliner main module (src/*.ml,
# unwrapped, exactly the upstream install layout), which our rules compile
# directly; ocamldep -sort recovers the module order. src/tool/ (the
# standalone `cmdliner` completion tool) is not built; Semgrep consumes the
# library only.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "cmdliner",
    srcs = glob([
        "src/*.ml",
        "src/*.mli",
    ]),
    visibility = ["//visibility:public"],
)
