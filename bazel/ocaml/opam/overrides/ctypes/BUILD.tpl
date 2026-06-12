# Override BUILD for ctypes (installed by extension.bzl).
#
# Why an override: src/ctypes/dune generates ctypes_primitives.ml with a
# dune-configurator probe (../configure/gen_c_primitives.exe) and copies
# ocaml_integers.h out of the installed integers package
# (%{lib:integers:ocaml_integers.h}); neither rule shape is modeled.
#
# The probe compiles C snippets to read sizeof/alignof/format strings for
# every C primitive. Unlike parmap's (whose two outputs are pinned), the
# generated file is ~130 lines, so the real probe runs in a genrule via the
# lwt.unix discover mold: built with our rules, run on the executor against
# the staged sysroot with dune's .dune/configurator.v2 protocol hand-written.
# The output is identical on this ruleset's two platforms (linux x86_64 and
# aarch64 are both little-endian LP64; long double is 16/16 on both), so the
# default-pool run serves both arch shards, exactly like lwt_features.
#
# ctypes.stubs is a plain second library. ctypes-foreign (libffi) and
# ctypes.top stay unbuilt: yaml's stubgen path needs only ctypes + stubs.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library")

_SYSROOT = "@homelab//bazel/ocaml/toolchain:ocaml_compiler"

ocaml_binary(
    name = "gen_c_primitives",
    srcs = ["src/configure/gen_c_primitives.ml"],
    deps = ["@ocaml_dune_configurator//:configurator"],
)

genrule(
    name = "ctypes_primitives_ml",
    srcs = [_SYSROOT],
    outs = ["ctypes_primitives.ml"],
    cmd = """
set -e
export LC_ALL=C
T=$$(mktemp -d) && tar -xf $(location %s) -C $$T
G=$$(realpath $(location :gen_c_primitives))
B=$$(mktemp -d) && mkdir -p $$B/.dune
OC=$$T/bin/ocamlc.opt
# .dune/configurator.v2: csexp of ((ocamlc <path>) (ocaml_config_vars ((k v)...)))
{
    printf '((6:ocamlc%%d:%%s)(17:ocaml_config_vars(' $${#OC} "$$OC"
    OCAMLLIB=$$T/lib/ocaml "$$OC" -config | while IFS=: read -r k v; do
        v="$${v# }"
        printf '(%%d:%%s%%d:%%s)' $${#k} "$$k" $${#v} "$$v"
    done
    printf ')))'
} > $$B/.dune/configurator.v2
W=$$(mktemp -d)
(cd $$W && INSIDE_DUNE=$$B OCAMLLIB=$$T/lib/ocaml PATH=$$T/bin:$$PATH "$$G") > $@
""" % _SYSROOT,
    tools = [":gen_c_primitives"],
)

# ctypes' dune copies the installed integers header next to its own sources
# (and installs it); the cstubs C headers include it as "ocaml_integers.h".
genrule(
    name = "ocaml_integers_h",
    srcs = ["@ocaml_integers//:src/ocaml_integers.h"],
    outs = ["ocaml_integers.h"],
    cmd = "cp $(location @ocaml_integers//:src/ocaml_integers.h) $@",
)

# The headers ctypes installs, for consumers that compile generated cstubs C
# outside this package (yaml's stubgen genrules stage these by basename).
filegroup(
    name = "c_headers",
    srcs = glob(["src/ctypes/*.h"]) + [":ocaml_integers_h"],
    visibility = ["//visibility:public"],
)

ocaml_library(
    name = "ctypes",
    srcs = glob([
        "src/ctypes/*.ml",
        "src/ctypes/*.mli",
    ]) + [":ctypes_primitives_ml"],
    c_headers = glob(["src/ctypes/*.h"]) + [":ocaml_integers_h"],
    c_srcs = [
        "src/ctypes/complex_stubs.c",
        "src/ctypes/ctypes_bigarrays.c",
        "src/ctypes/ctypes_roots.c",
        "src/ctypes/ldouble_stubs.c",
        "src/ctypes/managed_buffer_stubs.c",
        "src/ctypes/posix_types_stubs.c",
        "src/ctypes/raw_pointer_stubs.c",
        "src/ctypes/type_info_stubs.c",
    ],
    visibility = ["//visibility:public"],
    deps = ["@ocaml_integers//:integers"],
)

ocaml_library(
    name = "ctypes_stubs",
    srcs = glob([
        "src/cstubs/*.ml",
        "src/cstubs/*.mli",
    ]),
    opam_deps = ["str"],
    visibility = ["//visibility:public"],
    deps = [":ctypes"],
)
