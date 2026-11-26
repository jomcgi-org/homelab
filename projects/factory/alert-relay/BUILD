load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "alert-relay_lib",
    srcs = [
        "graph.go",
        "main.go",
    ],
    embedsrcs = ["ascii.bin"],
    importpath = "github.com/jomcgi/homelab/projects/factory/alert-relay",
    visibility = ["//visibility:private"],
)

go_binary(
    name = "alert-relay",
    embed = [":alert-relay_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "alert-relay_test",
    srcs = ["main_test.go"],
    data = ["testdata/graph.png"],
    embed = [":alert-relay_lib"],
)
