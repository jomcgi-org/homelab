load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_library(
    name = "signoz-dashboard-sidecar_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/platform/signoz-addons/dashboard-sidecar/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_prometheus_client_golang//prometheus:go_default_library",
        "@com_github_prometheus_client_golang//prometheus/promauto:go_default_library",
        "@com_github_prometheus_client_golang//prometheus/promhttp:go_default_library",
        "@io_k8s_api//core/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/api/errors:go_default_library",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/watch:go_default_library",
        "@io_k8s_client_go//kubernetes:go_default_library",
        "@io_k8s_client_go//rest:go_default_library",
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
        "@io_k8s_api//core/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/types:go_default_library",
        "@io_k8s_client_go//kubernetes/fake:go_default_library",
    ],
)

# Container image
go_image(
    name = "image",
    binary = ":signoz-dashboard-sidecar",
    repository = "ghcr.io/jomcgi/homelab/projects/platform/signoz-addons/dashboard-sidecar/cmd",
)
