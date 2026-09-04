load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "authentik",
    srcs = ["adapter.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider/authentik",
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
    deps = ["//projects/embervm/tokenbroker/internal/provider"],
)

go_test(
    name = "authentik_test",
    srcs = ["adapter_test.go"],
    embed = [":authentik"],
    deps = ["//projects/embervm/tokenbroker/internal/provider"],
)
