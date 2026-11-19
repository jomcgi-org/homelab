load("@rules_go//go:def.bzl", "go_library")

go_library(
    name = "runbookadmission",
    srcs = ["handler.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/runbookadmission",
    visibility = ["//projects/factory:__subpackages__"],
    deps = [
        "@io_k8s_api//admission/v1:admission",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes/typed/core/v1:core",
    ],
)
