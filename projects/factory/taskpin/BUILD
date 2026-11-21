load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "taskpin",
    srcs = ["http.go"],
    importpath = "github.com/jomcgi/homelab/projects/factory/taskpin",
    visibility = ["//projects/factory:__subpackages__"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/broker/auth",
    ],
)

go_test(
    name = "taskpin_test",
    srcs = ["http_test.go"],
    embed = [":taskpin"],
    deps = [
        "//projects/factory/broker/actorjwt",
        "//projects/factory/broker/auth",
        "@io_k8s_api//authentication/v1:authentication",
        "@io_k8s_apimachinery//pkg/runtime",
        "@io_k8s_client_go//kubernetes/fake",
        "@io_k8s_client_go//testing",
    ],
)
