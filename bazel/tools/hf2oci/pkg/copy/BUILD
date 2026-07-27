load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "copy",
    srcs = [
        "classify.go",
        "copy.go",
        "errors.go",
        "resolve.go",
    ],
    importpath = "github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/copy",
    visibility = ["//visibility:public"],
    deps = [
        "//bazel/tools/hf2oci/pkg/gguf",
        "//bazel/tools/hf2oci/pkg/hf",
        "//bazel/tools/hf2oci/pkg/oci",
        "//bazel/tools/hf2oci/pkg/ociref",
        "@com_github_google_go_containerregistry//pkg/authn",
        "@com_github_google_go_containerregistry//pkg/name",
        "@com_github_google_go_containerregistry//pkg/v1:pkg",
        "@com_github_google_go_containerregistry//pkg/v1/remote",
        "@com_github_google_go_containerregistry//pkg/v1/remote/transport",
        "@org_golang_x_sync//errgroup",
    ],
)

go_test(
    name = "copy_test",
    srcs = [
        "classify_test.go",
        "copy_test.go",
        "errors_test.go",
        "progress_test.go",
        "resolve_test.go",
    ],
    embed = [":copy"],
    deps = [
        "//bazel/tools/hf2oci/pkg/gguf",
        "//bazel/tools/hf2oci/pkg/hf",
        "@com_github_google_go_containerregistry//pkg/name",
        "@com_github_google_go_containerregistry//pkg/registry",
        "@com_github_google_go_containerregistry//pkg/v1/remote",
        "@com_github_google_go_containerregistry//pkg/v1/remote/transport",
        "@com_github_stretchr_testify//assert",
        "@com_github_stretchr_testify//require",
    ],
)
