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
        "@com_github_google_go_containerregistry//pkg/name:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/empty:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/mutate:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/remote:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/remote/transport:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/stream:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/tarball:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/types:go_default_library",
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
        "@com_github_google_go_containerregistry//pkg/name:go_default_library",
        "@com_github_google_go_containerregistry//pkg/registry:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1:go_default_library",
        "@com_github_google_go_containerregistry//pkg/v1/remote:go_default_library",
        "@com_github_stretchr_testify//assert:go_default_library",
        "@com_github_stretchr_testify//require:go_default_library",
    ],
)
