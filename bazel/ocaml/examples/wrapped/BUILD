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

# No member is named `widgets`, so there is no main module: the generated alias
# module *is* the public `Widgets` wrapper (dune's behavior). Consumers reach
# members as `Widgets.Button` / `Widgets.Config`.
ocaml_library(
    name = "widgets",
    srcs = glob(["widgets/*.ml"]),
    wrapped = True,
)

ocaml_test(
    name = "wrapped_test",
    srcs = ["wrapped_test.ml"],
    deps = [
        ":colors",
        ":shapes",
        ":widgets",
    ],
)
