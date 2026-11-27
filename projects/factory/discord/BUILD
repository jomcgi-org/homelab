load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "discord",
    srcs = [
        "agent.go",
        "backfill.go",
        "bridge.go",
        "discord.go",
        "gateway.go",
        "hindsight.go",
        "history.go",
        "ingest.go",
        "notification.go",
        "operator.go",
        "places.go",
        "policy.go",
        "poll.go",
        "render.go",
        "rest.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/kagent",
        "//projects/factory/connector/notify",
        "@com_github_prometheus_client_golang//prometheus",
        "@io_nhooyr_websocket//:websocket",
    ],
)

go_test(
    name = "discord_test",
    srcs = [
        "backfill_test.go",
        "bridge_test.go",
        "clients_test.go",
        "history_test.go",
        "ingest_test.go",
        "notification_test.go",
        "operator_test.go",
        "places_test.go",
        "poll_test.go",
        "render_test.go",
        "server_agents_test.go",
    ],
    embed = [":discord"],
    deps = [
        "//projects/factory/connector/kagent",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/testutil",
        "@io_nhooyr_websocket//:websocket",
    ],
)
