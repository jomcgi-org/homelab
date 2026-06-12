# Override BUILD for domain-local-await (installed by extension.bzl).
# Why an override: the dune file is a tuareg (OCaml-script) dune that adds
# threads.posix and the domain.ocaml4.ml Domain shim on pre-5 compilers; the
# sysroot is 5.3, so both conditionals pin to the plain OCaml 5 module set.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

ocaml_library(
    name = "domain_local_await",
    srcs = [
        "src/Domain_local_await.ml",
        "src/Domain_local_await.mli",
        "src/Thread_intf.ml",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = ["@ocaml_thread_table//:thread_table"],
)
