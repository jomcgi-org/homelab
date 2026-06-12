# Override BUILD for fmt (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. The real opam fmt
# (the vendored third_party/fmt 0.11.0 predates the lock and stays for its
# in-repo consumers). fmt.cli (cmdliner glue) is not built; nothing needs it.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "fmt",
    srcs = [
        "src/fmt.ml",
        "src/fmt.mli",
    ],
    visibility = ["//visibility:public"],
)

# findlib fmt.tty: TTY setup for fmt's stdout/stderr formatters.
ocaml_library(
    name = "fmt_tty",
    srcs = [
        "src/tty/fmt_tty.ml",
        "src/tty/fmt_tty.mli",
    ],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    deps = [":fmt"],
)
