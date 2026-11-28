load("@rules_go//go:def.bzl", "go_binary", "go_library")

go_library(
    name = "bosun-notes-view_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/discord/cmd/bosun-notes-view",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/discord/artifacts",
        "//projects/factory/discord/notesview",
    ],
)

go_binary(
    name = "bosun-notes-view",
    embed = [":bosun-notes-view_lib"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)
