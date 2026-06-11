load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_test")

# The case that motivates wrapping: both libraries ship a `Util` module. Flat
# compilation would produce two `Util` units that collide at link; wrapping
# namespaces them as Colors__Util / Shapes__Util behind alias modules.
ocaml_library(
    name = "colors",
    srcs = glob(["colors/*.ml"]),
    wrapped = True,
)

ocaml_library(
    name = "shapes",
    srcs = glob(["shapes/*.ml"]),
    wrapped = True,
)

ocaml_test(
    name = "wrapped_test",
    srcs = ["wrapped_test.ml"],
    deps = [
        ":colors",
        ":shapes",
    ],
)
