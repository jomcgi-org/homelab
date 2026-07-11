load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

bzl_library(
    name = "extensions",
    srcs = ["extensions.bzl"],
    visibility = ["//visibility:public"],
    deps = [
        ":digests",
        ":oci_archive",
    ],
)

# Platform-aware alias for the pro engine binary
alias(
    name = "engine",
    actual = select({
        "//bazel/semgrep/third_party/semgrep:linux_x86_64": "@semgrep_pro_engine_amd64//:engine",
        "//bazel/semgrep/third_party/semgrep:linux_aarch64": "@semgrep_pro_engine_arm64//:engine",
        "//bazel/semgrep/third_party/semgrep:macos_x86_64": "@semgrep_pro_engine_osx_x86_64//:engine",
        "//bazel/semgrep/third_party/semgrep:macos_aarch64": "@semgrep_pro_engine_osx_arm64//:engine",
    }),
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "digests",
    srcs = ["digests.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "oci_archive",
    srcs = ["oci_archive.bzl"],
    visibility = ["//visibility:public"],
)
