load("//bazel/ocaml:defs.bzl", "ocaml_test")

# Phase 6 end to end: the pcre2-ocaml bindings (whose C stub binds libpcre2)
# linked against the vendored PCRE2 C library through cc_deps. Proves the
# OCaml-stub -> cc_library path works on RBE with a real third-party C library.
# no-arm64: cc_library needs a C++ toolchain targeting aarch64; CI's RBE C++
# toolchain is x86_64-only, so cc-dependent examples stay off the arm64 shard.
ocaml_test(
    name = "pcre2_test",
    srcs = ["pcre2_test.ml"],
    tags = ["no-arm64"],
    deps = ["@ocaml_pcre2//:pcre2"],
)
