load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "server",
    srcs = [
        "ads.go",
        "http.go",
        "store.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/xds/server",
    visibility = ["//projects/embervm/xds:__subpackages__"],
    deps = [
        "//projects/embervm/xds/snapshot",
        "@com_github_envoyproxy_go_control_plane//pkg/cache/v3:cache",
        "@com_github_envoyproxy_go_control_plane//pkg/resource/v3:resource",
        "@com_github_envoyproxy_go_control_plane//pkg/server/v3:server",
        "@com_github_envoyproxy_go_control_plane_envoy//service/discovery/v3:discovery",
        "@org_golang_google_grpc//:grpc",
    ],
)

go_test(
    name = "server_test",
    srcs = [
        "ads_test.go",
        "http_test.go",
        "store_test.go",
    ],
    embed = [":server"],
    deps = [
        "//projects/embervm/xds/snapshot",
        "@com_github_envoyproxy_go_control_plane//pkg/resource/v3:resource",
        "@com_github_envoyproxy_go_control_plane_envoy//config/core/v3:core",
        "@com_github_envoyproxy_go_control_plane_envoy//service/discovery/v3:discovery",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//credentials/insecure",
        "@org_golang_google_grpc//test/bufconn",
    ],
)
