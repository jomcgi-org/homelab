# Override BUILD for parmap (installed by extension.bzl).
#
# Why an override: the dune tree generates parmap_compat.ml and
# setcore_stubs.h with a dune-configurator probe (config/discover.ml). Both
# outputs are compile-time constants on this ruleset's only platform (linux
# glibc, both arches), so the probe is pinned instead of run:
#
#   * parmap_compat.ml selects Unix.map_file on any OCaml >= 4.06 (the
#     Bigarray fallback is for 4.03-4.05; the sysroot is 5.3).
#   * setcore_stubs.h: sched_setaffinity has been in glibc since 2.3.4
#     (2004), and the mach thread-policy branch is darwin-only, which is
#     out of scope per ADR tooling/008.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "parmap_compat_ml",
    outs = ["parmap_compat.ml"],
    cmd = "printf 'let map_file = Unix.map_file\\n' > $@",
)

genrule(
    name = "setcore_stubs_h",
    outs = ["setcore_stubs.h"],
    cmd = "printf '#define HAVE_DECL_SCHED_SETAFFINITY 1\\n" +
          "#define HAVE_MACH_THREAD_POLICY_H 0\\n' > $@",
)

ocaml_library(
    name = "parmap",
    srcs = glob([
        "src/*.ml",
        "src/*.mli",
    ]) + [":parmap_compat_ml"],
    c_headers = [":setcore_stubs_h"],
    c_srcs = [
        "src/bytearray_stubs.c",
        "src/setcore_stubs.c",
    ],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
)
