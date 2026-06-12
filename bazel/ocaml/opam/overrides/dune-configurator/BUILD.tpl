# Override BUILD for dune-configurator (installed by extension.bzl).
#
# Why an override: configurator lives inside the dune source tree and its dune
# file uses (private_modules ...), a (:include flags/flags.sexp) computed by a
# bootstrap rule, and (special_builtin_support (configurator ...)) -- none of
# which dune2bazel models. All three are inert for our build: private_modules
# only filters the installed cmis, the computed flags are () on any
# OCaml >= 4.3, and special_builtin_support only matters to dune itself when
# it *runs* a configurator script (it writes the .dune/configurator.v2 file;
# our consumers generate that file by hand in a genrule, see
# overrides/lwt/BUILD.extra.tpl). So the library compiles as a plain wrapped
# ocaml_library.
#
# Only otherlibs/configurator/src is built; the rest of the dune tree is
# along for the download.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "configurator",
    srcs = glob([
        "otherlibs/configurator/src/*.ml",
        "otherlibs/configurator/src/*.mli",
        "otherlibs/configurator/src/*.mll",
    ]),
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_csexp//:csexp"],
)
