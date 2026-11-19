load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "hikes-writer_lib",
    srcs = [
        "catalogue.go",
        "forecast.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/cmd/hikes-writer",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/scotland-connector/internal/astro",
        "//projects/scotland-connector/internal/r2",
        "@com_github_google_uuid//:uuid",
        "@org_golang_x_net//html",
    ],
)

go_binary(
    name = "hikes-writer",
    embed = [":hikes-writer_lib"],
    visibility = ["//projects/hikes:__pkg__"],
)

go_test(
    name = "hikes-writer_test",
    srcs = ["writer_test.go"],
    embed = [":hikes-writer_lib"],
    deps = ["//projects/scotland-connector/internal/walks"],
)
