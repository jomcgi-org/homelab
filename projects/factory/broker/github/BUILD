load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "github",
    srcs = [
        "app.go",
        "minter.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/github",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/factory/broker/config",
        "//projects/factory/broker/metrics",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes",
    ],
)

go_test(
    name = "github_test",
    srcs = ["github_test.go"],
    embed = [":github"],
    deps = [
        "//projects/factory/broker/config",
        "//projects/factory/broker/metrics",
        "@com_github_prometheus_client_golang//prometheus/testutil",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes/fake",
    ],
)
