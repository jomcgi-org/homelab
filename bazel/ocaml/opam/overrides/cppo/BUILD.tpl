# Override BUILD for cppo (installed by extension.bzl).
#
# Why an override: cppo's dune uses ocamlyacc, a version-stamp (rule), and a
# per_module compat preprocess. The compat step is a no-op on OCaml >= 4.03
# (compat.ml prints shims only for older compilers), so it is skipped; the
# lexer/parser go through the driver's ocamllex/ocamlyacc support.
#
# cppo is a build-time tool only (the `cppo` attr on ocaml rules); nothing
# links it as a library.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary")

genrule(
    name = "version_src",
    outs = ["cppo_version.ml"],
    cmd = "printf 'let cppo_version = \"1.8.0\"\\n' > $@",
)

ocaml_binary(
    name = "cppo",
    srcs = glob(
        ["src/cppo_*.ml", "src/cppo_*.mli", "src/cppo_*.mll", "src/cppo_*.mly"],
        # cppo_version.ml is generated above; its .mli ships in the tree.
        exclude = ["src/cppo_version.ml"],
    ) + [":version_src"],
    opam_deps = [
        "unix",
        "str",
    ],
    visibility = ["//visibility:public"],
)
