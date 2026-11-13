load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "warnings-producer_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/cmd/warnings-producer",
    visibility = ["//visibility:private"],
    deps = ["//projects/scotland-connector/internal/warnings"],
)

go_binary(
    name = "warnings-producer",
    embed = [":warnings-producer_lib"],
    visibility = ["//visibility:public"],
)
