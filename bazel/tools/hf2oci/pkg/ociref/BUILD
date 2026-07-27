load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "ociref",
    srcs = ["ociref.go"],
    importpath = "github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/ociref",
    visibility = ["//visibility:public"],
    deps = ["//bazel/tools/hf2oci/pkg/hf"],
)

go_test(
    name = "ociref_test",
    srcs = ["ociref_test.go"],
    embed = [":ociref"],
    deps = [
        "//bazel/tools/hf2oci/pkg/hf",
        "@com_github_stretchr_testify//assert",
    ],
)
