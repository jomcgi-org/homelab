load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

# Whole-library compile/link driver, executed inside the pinned OCaml container.
sh_binary(
    name = "ocaml_compile",
    srcs = ["ocaml_compile.sh"],
    visibility = ["//visibility:public"],
)
