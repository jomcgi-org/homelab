load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "vsockhttp",
    srcs = ["transport.go"],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/node/vsockhttp",
    visibility = ["//projects/firecracker/substrate:__subpackages__"],
    deps = ["//projects/firecracker/substrate/vsockproto"],
)

go_test(
    name = "vsockhttp_test",
    srcs = ["transport_test.go"],
    embed = [":vsockhttp"],
    deps = ["//projects/firecracker/substrate/shim"],
)
