load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "auth.go",
        "main.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/noded/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/noded/config",
        "//projects/embervm/noded/fcvm/driver",
        "//projects/embervm/noded/server",
        "//projects/embervm/noded/serving",
        "//projects/embervm/noded/store",
        "//projects/embervm/noded/vsockhttp",
        "//projects/embervm/proto/embervm/node/v1:nodev1",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//metadata",
        "@org_golang_google_grpc//status",
    ],
)

go_binary(
    name = "noded",
    embed = [":cmd_lib"],
    visibility = ["//projects/embervm/noded:__subpackages__"],
)

go_test(
    name = "auth_test",
    srcs = ["auth_test.go"],
    embed = [":cmd_lib"],
    deps = [
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//metadata",
        "@org_golang_google_grpc//status",
    ],
)
