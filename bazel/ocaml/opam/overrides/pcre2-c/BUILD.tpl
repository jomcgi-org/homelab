# Override BUILD for the PCRE2 C library (installed by extension.bzl).
#
# Not an opam package: this is the upstream PCRE2 C source, vendored as a
# cc_library so the pcre2-ocaml bindings can link it hermetically (instead of
# the opam `conf-libpcre2` system-library probe). PCRE2 ships pre-generated
# .generic config/headers and a .dist chartables file, so it builds without
# autotools/cmake: copy those into place and compile the 8-bit core sources
# (JIT is left disabled, which guards out the sljit includes).
load("@rules_cc//cc:defs.bzl", "cc_library")

genrule(
    name = "config_h",
    srcs = ["src/config.h.generic"],
    outs = ["config.h"],
    cmd = "cp $(location src/config.h.generic) $@",
)

genrule(
    name = "pcre2_h",
    srcs = ["src/pcre2.h.generic"],
    outs = ["pcre2.h"],
    cmd = "cp $(location src/pcre2.h.generic) $@",
)

genrule(
    name = "chartables_c",
    srcs = ["src/pcre2_chartables.c.dist"],
    outs = ["pcre2_chartables.c"],
    cmd = "cp $(location src/pcre2_chartables.c.dist) $@",
)

cc_library(
    name = "pcre2",
    srcs = [
        "src/pcre2_auto_possess.c",
        "src/pcre2_chkdint.c",
        "src/pcre2_compile.c",
        "src/pcre2_config.c",
        "src/pcre2_context.c",
        "src/pcre2_convert.c",
        "src/pcre2_dfa_match.c",
        "src/pcre2_error.c",
        "src/pcre2_extuni.c",
        "src/pcre2_find_bracket.c",
        "src/pcre2_jit_compile.c",
        "src/pcre2_maketables.c",
        "src/pcre2_match.c",
        "src/pcre2_match_data.c",
        "src/pcre2_newline.c",
        "src/pcre2_ord2utf.c",
        "src/pcre2_pattern_info.c",
        "src/pcre2_script_run.c",
        "src/pcre2_serialize.c",
        "src/pcre2_string_utils.c",
        "src/pcre2_study.c",
        "src/pcre2_substitute.c",
        "src/pcre2_substring.c",
        "src/pcre2_tables.c",
        "src/pcre2_ucd.c",
        "src/pcre2_valid_utf.c",
        "src/pcre2_xclass.c",
        ":chartables_c",
        "config.h",
    ] + glob(["src/*.h"]),
    hdrs = [":pcre2_h"],
    # pcre2_jit_compile.c textually #includes these siblings (outside the
    # SUPPORT_JIT guard), so they must stage without being compiled standalone.
    textual_hdrs = [
        "src/pcre2_jit_match.c",
        "src/pcre2_jit_misc.c",
    ],
    copts = [
        "-DHAVE_CONFIG_H",
        "-DPCRE2_CODE_UNIT_WIDTH=8",
        "-DPCRE2_STATIC",
        "-DHAVE_MEMMOVE",
        "-w",
    ],
    # "." exports the generated config.h/pcre2.h (in the bin dir); "src" exports
    # the internal headers the .c files include.
    includes = [
        ".",
        "src",
    ],
    visibility = ["//visibility:public"],
)
