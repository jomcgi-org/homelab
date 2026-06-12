# Override BUILD for mtime (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. mtime is the
# span/timestamp arithmetic; mtime.clock.os reads the monotonic clock through
# a C stub (clock_gettime), riding c_srcs.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "mtime",
    srcs = [
        "src/mtime.ml",
        "src/mtime.mli",
    ],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "mtime_clock",
    srcs = [
        "src/clock/mtime_clock.ml",
        "src/clock/mtime_clock.mli",
    ],
    c_srcs = ["src/clock/mtime_clock_stubs.c"],
    visibility = ["//visibility:public"],
    deps = [":mtime"],
)
