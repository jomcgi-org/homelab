# Override BUILD for stdlib-shims (installed by bazel/ocaml/opam/extension.bzl).
#
# Why an override: upstream's dune file is a jbuild_plugin OCaml *program* that
# computes which shim modules the running compiler is missing. On every
# OCaml >= 4.11 that set is empty, and this toolchain's floor is far above
# (5.3 from source), so the package reduces to an empty library that exists
# only as a link target for packages that declare (libraries stdlib-shims).
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "empty_src",
    outs = ["stdlib_shims_empty.ml"],
    cmd = "printf '(* stdlib-shims provides no shims on OCaml >= 4.11. *)\\n' > $@",
)

ocaml_library(
    name = "stdlib_shims",
    srcs = [":empty_src"],
    visibility = ["//visibility:public"],
)
