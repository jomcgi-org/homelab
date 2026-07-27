load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_library(
    name = "ws-gateway_lib",
    srcs = [
        "auth.go",
        "client.go",
        "events.go",
        "hub.go",
        "main.go",
        "redis.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/grimoire/ws-gateway",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_redis_go_redis_v9//:go-redis",
        "@io_nhooyr_websocket//:websocket",
    ],
)

go_test(
    name = "ws-gateway_test",
    srcs = [
        "auth_test.go",
        "client_test.go",
        "events_test.go",
        "hub_test.go",
        "main_test.go",
        "redis_test.go",
    ],
    embed = [":ws-gateway_lib"],
    deps = [
        "@com_github_redis_go_redis_v9//:go-redis",
    ],
)

go_binary(
    name = "ws-gateway",
    embed = [":ws-gateway_lib"],
    visibility = ["//visibility:public"],
)

go_image(
    name = "image",
    binary = ":ws-gateway",
    repository = "ghcr.io/jomcgi/homelab/projects/grimoire/ws-gateway",
)
