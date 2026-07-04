load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "auth",
    srcs = [
        "auth.go",
        "reviewer.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/auth",
    visibility = ["//projects/firecracker/substrate:__subpackages__"],
    deps = [
        "@io_k8s_api//authentication/v1:authentication",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes",
        "@io_k8s_client_go//rest",
    ],
)

go_test(
    name = "auth_test",
    srcs = ["auth_test.go"],
    embed = [":auth"],
)
