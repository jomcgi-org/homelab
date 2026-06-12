# Override BUILD for the PCRE1 C library (installed by extension.bzl).
# Not an opam package: upstream PCRE 8.45 source, vendored as a cc_library so
# the pcre-ocaml bindings link it hermetically (the pcre2-c pattern). PCRE1
# ships pre-generated .generic config/headers and a .dist chartables file.
# UTF-8 and Unicode property support are compiled in (pure C, no deps);
# Semgrep's PCRE1 patterns rely on them.
load("@rules_cc//cc:defs.bzl", "cc_library")

genrule(
    name = "config_h",
    srcs = ["config.h.generic"],
    outs = ["config.h"],
    cmd = "cp $(location config.h.generic) $@",
)

genrule(
    name = "pcre_h",
    srcs = ["pcre.h.generic"],
    outs = ["pcre.h"],
    cmd = "cp $(location pcre.h.generic) $@",
)

genrule(
    name = "chartables_c",
    srcs = ["pcre_chartables.c.dist"],
    outs = ["pcre_chartables.c"],
    cmd = "cp $(location pcre_chartables.c.dist) $@",
)

cc_library(
    name = "pcre1",
    srcs = [
        "pcre_byte_order.c",
        "pcre_compile.c",
        "pcre_config.c",
        "pcre_dfa_exec.c",
        "pcre_exec.c",
        "pcre_fullinfo.c",
        "pcre_get.c",
        "pcre_globals.c",
        "pcre_jit_compile.c",
        "pcre_maketables.c",
        "pcre_newline.c",
        "pcre_ord2utf8.c",
        "pcre_refcount.c",
        "pcre_string_utils.c",
        "pcre_study.c",
        "pcre_tables.c",
        "pcre_ucd.c",
        "pcre_valid_utf8.c",
        "pcre_version.c",
        "pcre_xclass.c",
        ":chartables_c",
        "config.h",
    ] + glob(["*.h"]),
    hdrs = [":pcre_h"],
    copts = [
        "-DHAVE_CONFIG_H",
        "-DPCRE_STATIC",
        "-DHAVE_MEMMOVE",
        "-DSUPPORT_UTF",
        "-DSUPPORT_UCP",
        "-w",
    ],
    includes = ["."],
    visibility = ["//visibility:public"],
)
