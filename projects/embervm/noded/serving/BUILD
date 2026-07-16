load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "serving",
    srcs = [
        "alloc.go",
        "net.go",
        "probe.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/serving",
    visibility = ["//visibility:public"],
)

go_test(
    name = "serving_test",
    srcs = [
        "alloc_test.go",
        "net_test.go",
        "probe_test.go",
    ],
    embed = [":serving"],
)
