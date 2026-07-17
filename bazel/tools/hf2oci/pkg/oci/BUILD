load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "oci",
    srcs = [
        "image.go",
        "push.go",
    ],
    importpath = "github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/oci",
    visibility = ["//visibility:public"],
    deps = [
        "@com_github_google_go_containerregistry//pkg/name",
        "@com_github_google_go_containerregistry//pkg/v1:pkg",
        "@com_github_google_go_containerregistry//pkg/v1/empty",
        "@com_github_google_go_containerregistry//pkg/v1/mutate",
        "@com_github_google_go_containerregistry//pkg/v1/remote",
        "@com_github_google_go_containerregistry//pkg/v1/remote/transport",
        "@com_github_google_go_containerregistry//pkg/v1/stream",
        "@com_github_google_go_containerregistry//pkg/v1/tarball",
        "@com_github_google_go_containerregistry//pkg/v1/types",
    ],
)

go_test(
    name = "oci_test",
    srcs = [
        "image_test.go",
        "push_test.go",
    ],
    embed = [":oci"],
    deps = [
        "@com_github_google_go_containerregistry//pkg/name",
        "@com_github_google_go_containerregistry//pkg/registry",
        "@com_github_google_go_containerregistry//pkg/v1:pkg",
        "@com_github_google_go_containerregistry//pkg/v1/remote",
        "@com_github_stretchr_testify//assert",
        "@com_github_stretchr_testify//require",
    ],
)
