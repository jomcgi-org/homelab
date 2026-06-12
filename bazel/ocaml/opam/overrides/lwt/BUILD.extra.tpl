# lwt.unix -- hand-written fragment appended after the translated core BUILD
# (lock.json "override_extra"; see extension.bzl). The core `lwt` library above
# stays dune2bazel-translated; this fragment adds the parts of the dune tree
# the translator does not model:
#
#   * config/discover.ml, a dune-configurator feature probe that compiles C
#     test snippets and writes lwt_features.{h,ml} (+ two flags .sexp files we
#     do not consume, see below). We build it with our rules and run it in a
#     genrule on the executor, replicating dune's protocol by hand: configurator
#     run via `Configurator.main` demands INSIDE_DUNE=<dir> and reads
#     <dir>/.dune/configurator.v2, a canonical s-expression naming ocamlc and
#     its -config output. libev is pinned OFF (--use-libev false): no libev on
#     the executors, and Semgrep does not use it; lwt_engine.ml falls back to
#     its vanilla select() engine (guarded on _HAVE_LIBEV && libev_default).
#   * the (foreign_stubs) C set: every stub in src/unix + unix_c/ + windows_c/
#     (the windows ones compile to empty objects on unix, exactly as dune
#     builds them) with the library's own headers staged via c_headers.
#   * lwt_unix.cppo.ml{,i} / lwt_process.cppo.ml, handled by the driver's
#     cppo attr.
#
# The discovered unix_c_flags.sexp / unix_c_library_flags.sexp are declared
# (discover always writes them) but not consumed: on the linux executors they
# only carry -pthread/-fPIC, which the ocamlopt stub compile and the threads
# stdlib archive already cover.
#
# The genrule stages the *unconstrained* sysroot (it runs on the default pool,
# so the binaries always match the executor); the lwt_features outputs are
# glibc/kernel feature macros, identical for linux x86_64 and arm64, so both
# arch shards consume the same generated sources.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary")

_SYSROOT = "@homelab//bazel/ocaml/toolchain:ocaml_compiler"

ocaml_binary(
    name = "discover",
    srcs = ["src/unix/config/discover.ml"],
    deps = ["@ocaml_dune_configurator//:configurator"],
)

genrule(
    name = "lwt_features",
    srcs = [_SYSROOT],
    outs = [
        "lwt_features.h",
        "lwt_features.ml",
        "unix_c_flags.sexp",
        "unix_c_library_flags.sexp",
    ],
    cmd = """
set -e
export LC_ALL=C
T=$$(mktemp -d) && tar -xf $(location %s) -C $$T
D=$$(realpath $(location :discover))
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
(cd $$W && INSIDE_DUNE=$$B OCAMLLIB=$$T/lib/ocaml PATH=$$T/bin:$$PATH \\
    "$$D" --use-libev false)
cp $$W/lwt_features.h $$W/lwt_features.ml \\
    $$W/unix_c_flags.sexp $$W/unix_c_library_flags.sexp $(RULEDIR)/
""" % _SYSROOT,
    tools = [":discover"],
)

ocaml_library(
    name = "lwt_unix",
    srcs = glob([
        "src/unix/*.ml",
        "src/unix/*.mli",
    ]) + [":lwt_features.ml"],
    c_headers = glob([
        "src/unix/*.h",
        "src/unix/unix_c/*.h",
    ]) + [":lwt_features.h"],
    c_srcs = glob([
        "src/unix/*.c",
        "src/unix/unix_c/*.c",
        "src/unix/windows_c/*.c",
    ]),
    cppo = "@ocaml_cppo//:cppo",
    opam_deps = [
        "unix",
        "threads",
    ],
    visibility = ["//visibility:public"],
    deps = [
        ":lwt",
        "@ocaml_ocplib_endian//:ocplib_endian_bigstring",
    ],
)
