load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "egress",
    srcs = ["forwarder.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/internal/egress",
    visibility = ["//projects/firecracker/substrate/invoke:__subpackages__"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)

go_test(
    name = "egress_test",
    srcs = ["forwarder_test.go"],
    embed = [":egress"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)
