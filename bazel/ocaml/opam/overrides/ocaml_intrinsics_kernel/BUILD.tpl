# Override BUILD for ocaml_intrinsics_kernel (installed by extension.bzl).
#
# Why an override: the dune library carries (foreign_stubs ...) and a
# (js_of_ocaml ...) field, which dune2bazel does not model. Both map directly:
# the three C stubs ride c_srcs, and js_of_ocaml is inert for a native build.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "ocaml_intrinsics_kernel",
    srcs = glob([
        "src/*.ml",
        "src/*.mli",
    ]),
    c_srcs = glob(["src/*.c"]),
    visibility = ["//visibility:public"],
    wrapped = True,
)
