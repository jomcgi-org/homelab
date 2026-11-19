load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "reaper",
    srcs = [
        "lease.go",
        "reaper.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/internal/reaper",
    visibility = ["//projects/factory/connector:__subpackages__"],
    deps = [
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/types",
        "@io_k8s_client_go//kubernetes/typed/core/v1:core",
        "@org_golang_google_protobuf//encoding/protowire",
    ],
)

go_test(
    name = "reaper_test",
    srcs = ["reaper_test.go"],
    data = ["@postgres_test//:postgres"],
    embed = [":reaper"],
    deps = [
        "//projects/factory/connector/internal/pgtest",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_client_go//kubernetes/typed/core/v1:core",
        "@io_k8s_client_go//rest",
        "@org_golang_google_protobuf//encoding/protowire",
    ],
)
