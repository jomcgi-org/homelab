load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "discord",
    srcs = [
        "activity.go",
        "agent.go",
        "bridge.go",
        "discord.go",
        "gateway.go",
        "hindsight.go",
        "history.go",
        "policy.go",
        "rest.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/kagent",
        "@io_nhooyr_websocket//:websocket",
    ],
)

go_test(
    name = "discord_test",
    srcs = [
        "activity_test.go",
        "bridge_test.go",
        "clients_test.go",
        "history_test.go",
    ],
    embed = [":discord"],
    deps = [
        "//projects/factory/connector/kagent",
        "@io_nhooyr_websocket//:websocket",
    ],
)
