load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "grant",
    srcs = ["manager.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/grant",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/auth",
        "//projects/factory/broker/metrics",
        "//projects/shared/go/tokenbroker/broker",
        "//projects/shared/go/tokenbroker/provider",
        "//projects/shared/go/tokenbroker/provider/codexchatgpt",
        "//projects/shared/go/tokenbroker/store",
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes",
    ],
)

go_test(
    name = "grant_test",
    srcs = ["manager_test.go"],
    embed = [":grant"],
    deps = [
        "//projects/factory/broker/internal/fakes",
        "//projects/factory/broker/metrics",
        "//projects/shared/go/tokenbroker/provider/codexchatgpt",
        "//projects/shared/go/tokenbroker/store",
        "@com_github_prometheus_client_golang//prometheus/testutil",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/runtime",
        "@io_k8s_client_go//kubernetes/fake",
        "@io_k8s_client_go//testing",
    ],
)
