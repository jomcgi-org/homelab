# Override BUILD for pcre2-ocaml (installed by extension.bzl).
#
# Why an override: upstream's dune uses dune-configurator (discover.exe) to find
# a system libpcre2 via pkg-config. We bypass that and link the vendored PCRE2
# cc_library through cc_deps: the stub's `#include <pcre2.h>` resolves via the
# cc_library's exported header dir, and libpcre2-8.a links into any binary.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "pcre2",
    srcs = [
        "lib/pcre2.ml",
        "lib/pcre2.mli",
    ],
    c_srcs = ["lib/pcre2_stubs.c"],
    cc_deps = ["@ocaml_pcre2_c//:pcre2"],
    visibility = ["//visibility:public"],
)
