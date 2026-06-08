load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Extractor used by the @ocaml_sysroot repository rule. Exported (not a py_binary)
# because it runs via the host python3 during repository fetch.
exports_files(["extract_debs.py"])

bzl_library(
    name = "repositories",
    srcs = [
        "debs.bzl",
        "repositories.bzl",
    ],
    visibility = ["//visibility:public"],
)
