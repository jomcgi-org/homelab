load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "server",
    srcs = [
        "registry.go",
        "server.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/server",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = [
        "//projects/embervm/noded/config",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/proto/embervm/node/v1:nodev1",
        "@org_golang_google_grpc//:go_default_library",
        "@org_golang_google_grpc//codes:go_default_library",
        "@org_golang_google_grpc//status:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "server_test",
    srcs = ["server_test.go"],
    embed = [":server"],
    deps = [
        "//projects/embervm/noded/config",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/proto/embervm/node/v1:nodev1",
        "@org_golang_google_grpc//:go_default_library",
        "@org_golang_google_grpc//codes:go_default_library",
        "@org_golang_google_grpc//credentials/insecure:go_default_library",
        "@org_golang_google_grpc//status:go_default_library",
        "@org_golang_google_grpc//test/bufconn:go_default_library",
    ],
)
