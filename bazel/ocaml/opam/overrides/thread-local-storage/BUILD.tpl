# Override BUILD for thread-local-storage (installed by extension.bzl).
# Why an override: the dune tree generates its Atomic compat module with a
# bundled gen.exe (src/gen/gen.ml) keyed on the compiler version, plus an
# (env) stanza dune2bazel does not model. On the pinned 5.3 sysroot gen.exe
# always emits `include Stdlib.Atomic` and copies atomic.post412.mli, so the
# codegen is replicated as two trivial genrules.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

genrule(
    name = "atomic_ml",
    outs = ["atomic.ml"],
    cmd = "printf 'include Stdlib.Atomic' > $@",
)

genrule(
    name = "atomic_mli",
    srcs = ["src/gen/atomic.post412.mli"],
    outs = ["atomic.mli"],
    cmd = "cp $< $@",
)

ocaml_library(
    name = "thread_local_storage",
    srcs = [
        "src/thread_local_storage.ml",
        "src/thread_local_storage.mli",
        ":atomic_ml",
        ":atomic_mli",
    ],
    opam_deps = ["threads"],
    visibility = ["//visibility:public"],
    wrapped = True,
)
