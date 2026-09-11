load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Phase 6 end to end: the pcre2-ocaml bindings (whose C stub binds libpcre2)
# linked against the vendored PCRE2 C library through cc_deps. Proves the
# OCaml-stub -> cc_library path works on RBE with a real third-party C library.
ocaml_test(
    name = "pcre2_test",
    srcs = ["pcre2_test.ml"],
    deps = ["@ocaml_pcre2//:pcre2"],
)
