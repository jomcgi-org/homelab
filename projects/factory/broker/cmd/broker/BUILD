load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "broker_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/cmd/broker",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/broker/auth",
        "//projects/factory/broker/config",
        "//projects/factory/broker/github",
        "//projects/factory/broker/grant",
        "//projects/factory/broker/metrics",
        "//projects/factory/broker/relay",
        "//projects/shared/go/tokenbroker/metrics",
        "//projects/shared/go/tokenbroker/provider/codexchatgpt",
        "//projects/shared/go/tokenbroker/quota",
        "//projects/shared/go/tokenbroker/store",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/collectors",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@io_k8s_client_go//kubernetes",
        "@io_k8s_client_go//rest",
    ],
)

go_binary(
    name = "broker",
    embed = [":broker_lib"],
    visibility = ["//projects/factory/broker:__subpackages__"],
)

go_test(
    name = "broker_test",
    srcs = ["main_test.go"],
    embed = [":broker_lib"],
    deps = [
        "//projects/factory/broker/config",
        "//projects/factory/broker/internal/fakes",
        "//projects/factory/broker/metrics",
        "//projects/shared/go/tokenbroker/quota",
        "@io_k8s_api//authentication/v1:authentication",
        "@io_k8s_apimachinery//pkg/runtime",
        "@io_k8s_client_go//kubernetes/fake",
        "@io_k8s_client_go//testing",
    ],
)
