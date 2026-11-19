load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "stars-forecast_lib",
    srcs = [
        "forecast.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/scotland-connector/cmd/stars-forecast",
    visibility = ["//visibility:private"],
    deps = ["//projects/scotland-connector/internal/r2"],
)

go_binary(
    name = "stars-forecast",
    embed = [":stars-forecast_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "stars-forecast_test",
    srcs = ["forecast_test.go"],
    embed = [":stars-forecast_lib"],
)
