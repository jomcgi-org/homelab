load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "vsockproto",
    srcs = ["proto.go"],
    importpath = "github.com/jomcgi/homelab/projects/agent_platform/vsockproto",
    visibility = ["//visibility:public"],
)

go_test(
    name = "vsockproto_test",
    srcs = ["proto_test.go"],
    embed = [":vsockproto"],
)
