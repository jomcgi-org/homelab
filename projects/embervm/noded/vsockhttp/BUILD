load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "vsockhttp",
    srcs = ["transport.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/vsockhttp",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = ["//projects/embervm/noded/vsockproto"],
)

go_test(
    name = "vsockhttp_test",
    srcs = ["transport_test.go"],
    embed = [":vsockhttp"],
)
