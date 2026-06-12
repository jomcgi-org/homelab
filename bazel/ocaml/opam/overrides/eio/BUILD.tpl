# Override BUILD for eio (installed by extension.bzl).
# Why an override: four libraries across lib_eio with -open flags, C stubs
# with their own include dir, and lintcstubs (rule)s guarded by
# (enabled_if %{bin-available:...}) -- absent here, so provably inert.
# eio_main/eio_linux/eio_posix (the io_uring backends) are NOT built:
# Semgrep's commons references Eio and Eio_unix only.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_library")

# findlib eio.runtime_events: trace points (runtime_events is an OCaml 5
# stdlib library, resolved like unix/str).
ocaml_library(
    name = "eio_runtime_events",
    srcs = glob([
        "lib_eio/runtime_events/*.ml",
        "lib_eio/runtime_events/*.mli",
    ]),
    opam_deps = ["runtime_events"],
    visibility = ["//visibility:public"],
    wrapped = True,
)

# The eio__core wrapped unit (cancellation, fibers, promises).
ocaml_library(
    name = "eio__core",
    srcs = glob([
        "lib_eio/core/*.ml",
        "lib_eio/core/*.mli",
    ]),
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":eio_runtime_events",
        "@ocaml_fmt//:fmt",
        "@ocaml_hmap//:hmap",
        "@ocaml_lwt_dllist//:lwt_dllist",
        "@ocaml_optint//:optint",
    ],
)

ocaml_library(
    name = "eio",
    srcs = glob([
        "lib_eio/*.ml",
        "lib_eio/*.mli",
    ]),
    ocamlopt_flags = [
        "-open",
        "Eio__core",
        "-open",
        "Eio__core.Private",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":eio__core",
        "@ocaml_bigstringaf//:bigstringaf",
        "@ocaml_cstruct//:cstruct",
        "@ocaml_fmt//:fmt",
        "@ocaml_lwt_dllist//:lwt_dllist",
        "@ocaml_mtime//:mtime",
        "@ocaml_optint//:optint",
    ],
)

ocaml_library(
    name = "eio_utils",
    srcs = glob([
        "lib_eio/utils/*.ml",
        "lib_eio/utils/*.mli",
    ]),
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":eio",
        "@ocaml_domain_local_await//:domain_local_await",
        "@ocaml_fmt//:fmt",
        "@ocaml_optint//:optint",
        "@ocaml_psq//:psq",
    ],
)

ocaml_library(
    name = "eio_unix",
    srcs = glob([
        "lib_eio/unix/*.ml",
        "lib_eio/unix/*.mli",
    ]),
    c_headers = glob([
        "lib_eio/unix/*.h",
        "lib_eio/unix/include/*.h",
    ]),
    c_srcs = glob(["lib_eio/unix/*.c"]),
    opam_deps = [
        "unix",
        "threads",
    ],
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [
        ":eio",
        ":eio_utils",
        "@ocaml_mtime//:mtime_clock",
    ],
)
