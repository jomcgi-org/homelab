load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "cmd",
    srcs = [
        "copy.go",
        "output.go",
        "resolve.go",
        "root.go",
    ],
    importpath = "github.com/jomcgi/homelab/bazel/tools/hf2oci/cmd/hf2oci/cmd",
    visibility = ["//visibility:public"],
    deps = [
        "//bazel/tools/hf2oci/pkg/copy",
        "//bazel/tools/hf2oci/pkg/hf",
        "@com_github_spf13_cobra//:cobra",
    ],
)

go_test(
    name = "cmd_test",
    srcs = [
        "copy_test.go",
        "output_test.go",
        "resolve_test.go",
        "root_test.go",
    ],
    embed = [":cmd"],
    deps = [
        "//bazel/tools/hf2oci/pkg/copy",
        "@com_github_spf13_cobra//:cobra",
        "@com_github_stretchr_testify//assert",
        "@com_github_stretchr_testify//require",
    ],
)
