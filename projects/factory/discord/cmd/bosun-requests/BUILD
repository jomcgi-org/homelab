load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "bosun-requests_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/cmd/bosun-requests",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/discord/requests",
        "@com_github_lib_pq//:pq",
    ],
)

go_binary(
    name = "bosun-requests",
    embed = [":bosun-requests_lib"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)
