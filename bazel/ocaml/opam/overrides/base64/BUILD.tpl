# Override BUILD for base64 (installed by extension.bzl).
# Why an override: src/dune filters (modules unsafe base64) and generates
# unsafe.ml with (rule (copy %{read:../config/which-unsafe-file} unsafe.ml)),
# a configurator probe selecting unsafe_pre407.ml on OCaml < 4.07 and
# unsafe_stable.ml otherwise. On the pinned 5.3 sysroot the probe is provably
# constant (the parmap pattern), so unsafe.ml is staged from unsafe_stable.ml
# by a genrule and the module lists are written out explicitly.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "unsafe_ml",
    srcs = ["src/unsafe_stable.ml"],
    outs = ["unsafe.ml"],
    cmd = "cp $(location src/unsafe_stable.ml) $@",
)

ocaml_library(
    name = "base64",
    srcs = [
        "src/base64.ml",
        "unsafe.ml",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
)

ocaml_library(
    name = "base64_rfc2045",
    srcs = ["src/base64_rfc2045.ml"],
    visibility = ["//visibility:public"],
    wrapped = True,
)
