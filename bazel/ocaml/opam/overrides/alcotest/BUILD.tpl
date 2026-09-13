# Override BUILD for Alcotest 1.9.1 (installed by extension.bzl).
#
# Why an override: Alcotest's dune tree selects callsite_loc.ml with an
# OCaml-version predicate and the public library carries a C stub plus a
# js_of_ocaml field. The pinned compiler is OCaml 5.3, so the >= 4.12 source is
# selected explicitly. JavaScript runtime metadata is irrelevant to this
# native-only closure; the native C stub is compiled and linked normally.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "callsite_loc_ml",
    srcs = ["src/alcotest-engine/callsite_loc.412.ml"],
    outs = ["callsite_loc.ml"],
    cmd = "cp $(location src/alcotest-engine/callsite_loc.412.ml) $@",
)

ocaml_library(
    name = "alcotest_stdlib_ext",
    srcs = glob([
        "src/alcotest-stdlib-ext/*.ml",
        "src/alcotest-stdlib-ext/*.mli",
    ]),
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        "@ocaml_astring//:astring",
        "@ocaml_cmdliner//:cmdliner",
        "@ocaml_uutf//:uutf",
    ],
)

ocaml_library(
    name = "alcotest_engine",
    srcs = glob(
        [
            "src/alcotest-engine/*.ml",
            "src/alcotest-engine/*.mli",
        ],
        exclude = ["src/alcotest-engine/callsite_loc.412.ml"],
    ) + [":callsite_loc_ml"],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":alcotest_stdlib_ext",
        "@ocaml_astring//:astring",
        "@ocaml_cmdliner//:cmdliner",
        "@ocaml_fmt//:fmt",
        "@ocaml_fmt//:fmt_cli",
        "@ocaml_re//:re",
        "@ocaml_stdlib_shims//:stdlib_shims",
        "@ocaml_uutf//:uutf",
    ],
)

ocaml_library(
    name = "alcotest",
    srcs = glob([
        "src/alcotest/*.ml",
        "src/alcotest/*.mli",
    ]),
    c_srcs = ["src/alcotest/alcotest_stubs.c"],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":alcotest_engine",
        "@ocaml_astring//:astring",
        "@ocaml_fmt//:fmt",
        "@ocaml_fmt//:fmt_tty",
    ],
)
