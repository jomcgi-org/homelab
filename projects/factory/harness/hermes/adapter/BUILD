load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "adapter",
    srcs = [
        "a2a.go",
        "config.go",
        "gateway.go",
        "hermes.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/harness/hermes/adapter",
    visibility = ["//projects/factory/harness/hermes:__subpackages__"],
    deps = [
        "@com_github_google_uuid//:uuid",
        "@in_gopkg_yaml_v3//:yaml_v3",
    ],
)

go_test(
    name = "adapter_test",
    srcs = ["adapter_test.go"],
    embed = [":adapter"],
    deps = [
        "//projects/factory/connector/kagent",
        "@in_gopkg_yaml_v3//:yaml_v3",
    ],
)
