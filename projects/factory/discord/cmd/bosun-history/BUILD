load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "bosun-history_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/cmd/bosun-history",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/discord/archive",
        "@com_github_lib_pq//:pq",
    ],
)

go_binary(
    name = "bosun-history",
    embed = [":bosun-history_lib"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)
