load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "notes",
    srcs = [
        "mcp.go",
        "notes.go",
        "places.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/notes",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/discord/artifacts",
    ],
)

go_test(
    name = "notes_test",
    srcs = ["notes_test.go"],
    embed = [":notes"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/discord/artifacts",
    ],
)
