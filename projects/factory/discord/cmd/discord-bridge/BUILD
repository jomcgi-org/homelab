load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "discord-bridge_lib",
    srcs = [
        "archive.go",
        "backfill.go",
        "main.go",
        "operator.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/cmd/discord-bridge",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/connector/kagent",
        "//projects/factory/discord",
        "//projects/factory/discord/archive",
        "@com_github_lib_pq//:pq",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@org_golang_x_time//rate",
    ],
)

go_binary(
    name = "discord-bridge",
    embed = [":discord-bridge_lib"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)

go_test(
    name = "discord-bridge_test",
    srcs = ["backfill_test.go"],
    embed = [":discord-bridge_lib"],
    deps = ["//projects/factory/discord"],
)
