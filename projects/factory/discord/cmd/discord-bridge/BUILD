load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "discord-bridge_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/cmd/discord-bridge",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/connector/kagent",
        "//projects/factory/discord",
    ],
)

go_binary(
    name = "discord-bridge",
    embed = [":discord-bridge_lib"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)
