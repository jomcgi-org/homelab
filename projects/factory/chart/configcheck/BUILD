load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "configcheck_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/chart/configcheck",
    visibility = ["//visibility:private"],
    deps = ["//projects/factory/reviewer/config"],
)

go_binary(
    name = "configcheck",
    embed = [":configcheck_lib"],
    visibility = ["//projects/factory/chart:__pkg__"],
)
