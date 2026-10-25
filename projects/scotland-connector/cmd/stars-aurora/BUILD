load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "stars-aurora_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/cmd/stars-aurora",
    visibility = ["//visibility:private"],
    deps = ["//projects/scotland-connector/internal/aurora"],
)

go_binary(
    name = "stars-aurora",
    embed = [":stars-aurora_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "stars-aurora_test",
    srcs = ["main_test.go"],
    embed = [":stars-aurora_lib"],
    deps = [
        "//projects/scotland-connector/internal/aurora",
        "//projects/scotland-connector/internal/fixture",
    ],
)
