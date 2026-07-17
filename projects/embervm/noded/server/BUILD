load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "server",
    srcs = [
        "group.go",
        "group_member.go",
        "group_registry.go",
        "registry.go",
        "server.go",
        "serving.go",
        "serving_registry.go",
        "stateful.go",
        "stateful_registry.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/server",
    visibility = ["//projects/embervm/noded:__subpackages__"],
    deps = [
        "//projects/embervm/noded/config",
        "//projects/embervm/noded/groupclock",
        "//projects/embervm/noded/serving",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/noded/volume",
        "//projects/embervm/proto/embervm/node/v1:nodev1",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//status",
        "@org_golang_x_sys//unix",
    ],
)

go_test(
    name = "server_test",
    srcs = [
        "group_member_test.go",
        "group_test.go",
        "server_test.go",
        "serving_test.go",
        "stateful_test.go",
    ],
    embed = [":server"],
    deps = [
        "//projects/embervm/noded/config",
        "//projects/embervm/noded/serving",
        "//projects/embervm/noded/substrate",
        "//projects/embervm/noded/volume",
        "//projects/embervm/proto/embervm/node/v1:nodev1",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//credentials/insecure",
        "@org_golang_google_grpc//status",
        "@org_golang_google_grpc//test/bufconn",
    ],
)
