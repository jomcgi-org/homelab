load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "kagent",
    srcs = [
        "a2a.go",
        "session.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/factory/connector/kagent",
    visibility = ["//visibility:public"],
    deps = [
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//credentials/insecure",
        "@org_golang_google_grpc//encoding",
        "@org_golang_google_grpc//mem",
        "@org_golang_google_grpc//status",
        "@org_golang_google_protobuf//encoding/protowire",
    ],
)

go_test(
    name = "kagent_test",
    srcs = ["a2a_test.go"],
    embed = [":kagent"],
    deps = [
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//metadata",
    ],
)
