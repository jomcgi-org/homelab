load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "store",
    srcs = ["secret.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store",
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
    deps = [
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes",
    ],
)

go_test(
    name = "store_test",
    srcs = ["secret_test.go"],
    embed = [":store"],
    deps = [
        "@io_k8s_api//core/v1:core",
        "@io_k8s_client_go//kubernetes/fake",
    ],
)
