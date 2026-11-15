load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "relay",
    srcs = ["relay.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/relay",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/broker/auth",
        "//projects/factory/broker/grant",
        "//projects/factory/broker/metrics",
        "//projects/shared/go/tokenbroker/provider/codexchatgpt",
        "//projects/shared/go/tokenbroker/quota",
    ],
)

go_test(
    name = "relay_test",
    srcs = ["relay_test.go"],
    embed = [":relay"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/broker/grant",
        "//projects/factory/broker/internal/fakes",
        "//projects/factory/broker/metrics",
        "//projects/shared/go/tokenbroker/provider/codexchatgpt",
        "//projects/shared/go/tokenbroker/quota",
        "//projects/shared/go/tokenbroker/store",
        "@com_github_prometheus_client_golang//prometheus/testutil",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes/fake",
    ],
)
