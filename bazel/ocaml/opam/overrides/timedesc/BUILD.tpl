# Override BUILD for timedesc (installed by extension.bzl).
# Why an override: a (rule) copies the pre-generated time_zone_constants.ml
# out of gen-artifacts/, and the library's warning spec rides through
# ocamlopt_flags verbatim.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "time_zone_constants_ml",
    srcs = ["gen-artifacts/time_zone_constants.ml"],
    outs = ["time_zone_constants.ml"],
    cmd = "cp $(location gen-artifacts/time_zone_constants.ml) $@",
)

ocaml_library(
    name = "timedesc",
    srcs = glob([
        "timedesc/*.ml",
        "timedesc/*.mli",
    ]) + [":time_zone_constants_ml"],
    ocamlopt_flags = [
        "-w",
        "+a-4-9-29-37-40-42-44-48-50-70@8",
    ],
    opam_deps = ["unix"],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        "@ocaml_angstrom//:angstrom",
        "@ocaml_ptime//:ptime",
        "@ocaml_timedesc_tzdb//:timedesc_tzdb",
        "@ocaml_timedesc_tzlocal//:timedesc_tzlocal",
    ],
)
