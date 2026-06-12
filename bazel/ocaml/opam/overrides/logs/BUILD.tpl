# Override BUILD for logs (installed by bazel/ocaml/opam/extension.bzl).
#
# Why an override: logs is not a dune project (topkg/ocamlbuild), so there are
# no dune files to translate. Each findlib sublibrary is a single module:
#
#   logs      -> Logs      (src/logs.ml, stdlib only)
#   logs.fmt  -> Logs_fmt  (ANSI/level reporters over the opam fmt)
#   logs.cli  -> Logs_cli  (cmdliner term for --verbosity)
#
# logs.lwt / logs.browser / logs.threaded are deliberately not built (they need
# lwt / js_of_ocaml / threads); add them when their deps enter the lock.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "logs",
    srcs = [
        "src/logs.ml",
        "src/logs.mli",
    ],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "logs_fmt",
    srcs = [
        "src/fmt/logs_fmt.ml",
        "src/fmt/logs_fmt.mli",
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":logs",
        "@ocaml_fmt//:fmt",
    ],
)

ocaml_library(
    name = "logs_cli",
    srcs = [
        "src/cli/logs_cli.ml",
        "src/cli/logs_cli.mli",
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":logs",
        "@ocaml_cmdliner//:cmdliner",
    ],
)
