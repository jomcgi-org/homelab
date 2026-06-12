# Override BUILD for thread-table (installed by extension.bzl).
# Why an override: a (rule) with (enabled_if %{arch_sixtyfour}) picks the
# 64-bit or 32-bit hash mixer. Both executor arches are 64-bit, so the choice
# is pinned to mix.64.ml.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "mix_ml",
    srcs = ["src/mix.64.ml"],
    outs = ["mix.ml"],
    cmd = "cp $(location src/mix.64.ml) $@",
)

ocaml_library(
    name = "thread_table",
    srcs = [
        "src/thread_table.ml",
        "src/thread_table.mli",
        ":mix_ml",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
)
