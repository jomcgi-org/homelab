load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_library(
    name = "signoz-dashboard-sidecar_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/platform/signoz-addons/dashboard-sidecar/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/promauto",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/watch",
        "@io_k8s_client_go//kubernetes",
        "@io_k8s_client_go//rest",
    ],
)

go_binary(
    name = "signoz-dashboard-sidecar",
    embed = [":signoz-dashboard-sidecar_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = ["main_test.go"],
    embed = [":signoz-dashboard-sidecar_lib"],  # keep
    deps = [
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/types",
        "@io_k8s_client_go//kubernetes/fake",
    ],
)

# Container image
go_image(
    name = "image",
    binary = ":signoz-dashboard-sidecar",
    repository = "ghcr.io/jomcgi/homelab/projects/platform/signoz-addons/dashboard-sidecar/cmd",
)
