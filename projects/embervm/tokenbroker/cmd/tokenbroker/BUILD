load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "tokenbroker_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/cmd/tokenbroker",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/tokenbroker/internal/broker",
        "//projects/embervm/tokenbroker/internal/metrics",
        "//projects/embervm/tokenbroker/internal/provider",
        "//projects/embervm/tokenbroker/internal/provider/codexchatgpt",
        "//projects/embervm/tokenbroker/internal/store",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@io_k8s_client_go//kubernetes",
        "@io_k8s_client_go//rest",
    ],
)

go_binary(
    name = "tokenbroker",
    embed = [":tokenbroker_lib"],
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
)

go_test(
    name = "tokenbroker_test",
    srcs = ["main_test.go"],
    embed = [":tokenbroker_lib"],
    deps = [
        "//projects/embervm/tokenbroker/internal/broker",
        "//projects/embervm/tokenbroker/internal/provider",
        "//projects/embervm/tokenbroker/internal/store",
    ],
)

