# Override BUILD for astring (installed by extension.bzl).
# Why an override: topkg/Bünzli build, no dune to translate. Flat module set;
# the *_top toplevel-support modules are not built (no toplevel here).
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "astring",
    srcs = glob(
        [
            "src/*.ml",
            "src/*.mli",
        ],
        exclude = ["src/astring_top*"],
    ),
    visibility = ["//visibility:public"],
)
