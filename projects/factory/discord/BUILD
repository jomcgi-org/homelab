load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "discord",
    srcs = [
        "agent.go",
        "bridge.go",
        "discord.go",
        "gateway.go",
        "hindsight.go",
        "history.go",
        "ingest.go",
        "notification.go",
        "operator.go",
        "policy.go",
        "rest.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/connector/kagent",
        "//projects/factory/connector/notify",
        "@io_nhooyr_websocket//:websocket",
    ],
)

go_test(
    name = "discord_test",
    srcs = [
        "bridge_test.go",
        "clients_test.go",
        "history_test.go",
        "ingest_test.go",
        "notification_test.go",
        "operator_test.go",
    ],
    embed = [":discord"],
    deps = [
        "//projects/factory/connector/kagent",
        "@io_nhooyr_websocket//:websocket",
    ],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = ["ARCHITECTURE.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
