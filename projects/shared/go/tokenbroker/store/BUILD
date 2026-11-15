load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "store",
    srcs = [
        "quota_configmap.go",
        "secret.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store",
    visibility = ["//visibility:public"],
    deps = [
        "//projects/shared/go/tokenbroker/quota",
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes",
    ],
)

go_test(
    name = "store_test",
    srcs = [
        "quota_configmap_test.go",
        "secret_test.go",
    ],
    embed = [":store"],
    deps = [
        "//projects/shared/go/tokenbroker/quota",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes/fake",
    ],
)
