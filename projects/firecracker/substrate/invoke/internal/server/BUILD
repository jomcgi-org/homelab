load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "server",
    srcs = ["server.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/internal/server",
    visibility = ["//projects/firecracker/substrate/invoke:__subpackages__"],
)

go_test(
    name = "server_test",
    srcs = ["server_test.go"],
    embed = [":server"],
)
