load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "snapshot",
    srcs = ["desired.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/xds/snapshot",
    visibility = ["//projects/embervm/xds:__subpackages__"],
    deps = [
        "@com_github_envoyproxy_go_control_plane//pkg/cache/types:go_default_library",
        "@com_github_envoyproxy_go_control_plane//pkg/cache/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane//pkg/resource/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/cluster/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/core/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/endpoint/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/listener/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/route/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//extensions/filters/network/tcp_proxy/v3:go_default_library",
        "@org_golang_google_protobuf//types/known/anypb:go_default_library",
        "@org_golang_google_protobuf//types/known/durationpb:go_default_library",
        "@org_golang_google_protobuf//types/known/wrapperspb:go_default_library",
    ],
)

go_test(
    name = "snapshot_test",
    srcs = ["desired_test.go"],
    embed = [":snapshot"],
    deps = [
        "@com_github_envoyproxy_go_control_plane//pkg/resource/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/cluster/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/core/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/endpoint/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/listener/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//config/route/v3:go_default_library",
        "@com_github_envoyproxy_go_control_plane_envoy//extensions/filters/network/tcp_proxy/v3:go_default_library",
    ],
)
