load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "egress",
    srcs = ["forwarder.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/egress",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = ["//projects/embervm/noded/vsockproto"],
)

go_test(
    name = "egress_test",
    srcs = ["forwarder_test.go"],
    embed = [":egress"],
    deps = ["//projects/embervm/noded/vsockproto"],
)
