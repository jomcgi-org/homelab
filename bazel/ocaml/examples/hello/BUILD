load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library")

# Three modules with a real inter-module dependency (greeting -> message), so
# `ocamldep -sort` has ordering work to do.
ocaml_library(
    name = "hello_lib",
    srcs = [
        "greeting.ml",
        "greeting.mli",
        "message.ml",
    ],
)

# Native executable: links the local library, the vendored `fmt` opam dep, and
# the `unix` findlib package (opam_deps).
ocaml_binary(
    name = "hello",
    srcs = ["main.ml"],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    deps = [
        ":hello_lib",
        "//bazel/ocaml/third_party/fmt",
    ],
)

# Primary green-CI signal: the whole pipeline (ocamldep sort, per-module
# compile, archive, link) builds on RBE.
build_test(
    name = "hello_build_test",
    targets = [":hello"],
)

# Runtime verification: execute the native binary and check its output. The
# binary links the host's glibc, so it runs anywhere the build action ran.
sh_test(
    name = "hello_run_test",
    srcs = ["run_hello_test.sh"],
    args = ["$(rootpath :hello)"],
    data = [":hello"],
)
