load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "auth",
    srcs = [
        "auth.go",
        "cache.go",
        "reviewer.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/auth",
    visibility = ["//projects/firecracker/substrate:__subpackages__"],
    deps = [
        "@io_k8s_api//authentication/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:go_default_library",
        "@io_k8s_client_go//kubernetes:go_default_library",
        "@io_k8s_client_go//rest:go_default_library",
        "@io_opentelemetry_go_otel//:go_default_library",
        "@io_opentelemetry_go_otel//codes:go_default_library",
        "@io_opentelemetry_go_otel//propagation:go_default_library",
        "@org_golang_x_sync//singleflight:go_default_library",
    ],
)

go_test(
    name = "auth_test",
    srcs = [
        "auth_test.go",
        "cache_test.go",
    ],
    embed = [":auth"],
)
