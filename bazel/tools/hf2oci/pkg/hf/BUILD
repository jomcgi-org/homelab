load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "hf",
    srcs = [
        "client.go",
        "parallel.go",
        "types.go",
    ],
    importpath = "github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/hf",
    visibility = ["//visibility:public"],
)

go_test(
    name = "hf_test",
    srcs = [
        "client_test.go",
        "parallel_test.go",
    ],
    embed = [":hf"],
    deps = [
        "@com_github_stretchr_testify//assert",
        "@com_github_stretchr_testify//require",
    ],
)
