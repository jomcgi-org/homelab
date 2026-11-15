load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "auth",
    srcs = ["tokenreview.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/broker/auth",
    visibility = ["//visibility:public"],
    deps = [
        "@io_k8s_api//authentication/v1:authentication",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes",
    ],
)

go_test(
    name = "auth_test",
    srcs = ["tokenreview_test.go"],
    embed = [":auth"],
    deps = [
        "@io_k8s_api//authentication/v1:authentication",
        "@io_k8s_apimachinery//pkg/runtime",
        "@io_k8s_client_go//kubernetes/fake",
        "@io_k8s_client_go//testing",
    ],
)
