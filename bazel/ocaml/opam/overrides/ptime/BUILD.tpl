# Override BUILD for ptime (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. Two modules:
# ptime, plus the ptime.clock.os POSIX clock (one C stub) that the
# opentelemetry core timestamps with. The toplevel sublibrary is not built.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "ptime",
    srcs = [
        "src/ptime.ml",
        "src/ptime.mli",
    ],
    visibility = ["//visibility:public"],
)

# findlib ptime.clock.os.
ocaml_library(
    name = "ptime_clock",
    srcs = [
        "src/clock/ptime_clock.ml",
        "src/clock/ptime_clock.mli",
    ],
    c_srcs = ["src/clock/ptime_clock_stubs.c"],
    visibility = ["//visibility:public"],
    deps = [":ptime"],
)
