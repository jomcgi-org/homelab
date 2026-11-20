load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_library(
    name = "credential-provider_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/credential-provider",
    visibility = ["//visibility:private"],
)

go_binary(
    name = "credential-provider",
    embed = [":credential-provider_lib"],
    pure = "on",
    static = "on",
    visibility = ["//visibility:public"],
)

go_test(
    name = "credential-provider_test",
    srcs = ["main_test.go"],
    embed = [":credential-provider_lib"],
)

# The init container copies the binary into atelet's plugin directory with
# `/opt/app install <dest>`.
go_image(
    name = "image",
    binary = ":credential-provider",
    repository = "ghcr.io/jomcgi/homelab/projects/platform-factory/substrate/credential-provider",
)

# The pushed image's digest, which resources/ pins and resources_test checks.
filegroup(
    name = "image_digest",
    srcs = [":image.digest"],
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)
