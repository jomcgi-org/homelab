load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "bosun-artifacts_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/cmd/bosun-artifacts",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/discord/artifacts",
        "//projects/factory/discord/notes",
    ],
)

go_binary(
    name = "bosun-artifacts",
    embed = [":bosun-artifacts_lib"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)
