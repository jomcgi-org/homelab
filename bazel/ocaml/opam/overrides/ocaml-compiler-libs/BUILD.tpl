# Override BUILD for ocaml-compiler-libs (installed by extension.bzl).
#
# Why an override: this package *re-packages* the compiler's own compiler-libs.
# Its dune tree generates ocaml_common.ml (re-exports of every unit in
# ocamlcommon.cma) and ocaml_shadow.ml (deprecation shadows for direct
# compiler-libs access) by introspecting the toolchain at build time. We run
# the same upstream generators against the sysroot tar, so the output tracks
# our pinned compiler exactly and adapts on compiler bumps.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library")

# Parses .cma archives via the running compiler's compiler-libs.
ocaml_library(
    name = "read_cma",
    srcs = glob(["src/read_cma/*.ml", "src/read_cma/*.mli"], allow_empty = True),
    opam_deps = [
        "compiler-libs.common",
        "compiler-libs.bytecomp",
    ],
)

ocaml_binary(
    name = "gen",
    srcs = ["src/gen/gen.ml"],
    deps = [":read_cma"],
)

ocaml_binary(
    name = "shadow_gen",
    srcs = ["src/shadow/gen/gen.ml"],
    deps = [":read_cma"],
)

# Both generators read the sysroot's compiler-libs; the tar is the toolchain's
# single source of truth for it (see bazel/ocaml/toolchain/compiler.bzl).
_SYSROOT = "@homelab//bazel/ocaml/toolchain:ocaml_compiler"

genrule(
    name = "ocaml_common_ml",
    srcs = [_SYSROOT],
    outs = ["ocaml_common.ml"],
    cmd = "T=$$(mktemp -d) && tar -xf $(location %s) -C $$T ./lib/ocaml/compiler-libs && " % _SYSROOT +
          "$(location :gen) -archive $$T/lib/ocaml/compiler-libs/ocamlcommon.cma -o $@",
    tools = [":gen"],
)

genrule(
    name = "ocaml_shadow_ml",
    srcs = [_SYSROOT],
    outs = ["ocaml_shadow.ml"],
    cmd = "T=$$(mktemp -d) && tar -xf $(location %s) -C $$T ./lib/ocaml/compiler-libs && " % _SYSROOT +
          "$(location :shadow_gen) -dir $$T/lib/ocaml/compiler-libs -o $@",
    tools = [":shadow_gen"],
)

ocaml_library(
    name = "ocaml_common",
    srcs = [":ocaml_common_ml"],
    opam_deps = ["compiler-libs.common"],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ocaml_shadow",
    srcs = [":ocaml_shadow_ml"],
    ocamlopt_flags = [
        "-w",
        "-49",
    ],
    deps = [":ocaml_common"],
    visibility = ["//visibility:public"],
)
