load("@rules_go//go:def.bzl", "go_binary", "go_library")

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
        "@org_golang_google_grpc//:go_default_library",
        "@org_golang_google_grpc//codes:go_default_library",
        "@org_golang_google_grpc//metadata:go_default_library",
        "@org_golang_google_grpc//status:go_default_library",
    ],
)

go_binary(
    name = "noded",
    embed = [":cmd_lib"],
    visibility = ["//projects/embervm/noded:__subpackages__"],
)
