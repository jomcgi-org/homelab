load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "snapshot",
    srcs = ["desired.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/xds/snapshot",
    visibility = ["//projects/embervm/xds:__subpackages__"],
    deps = [
        "@com_github_envoyproxy_go_control_plane//pkg/cache/types",
        "@com_github_envoyproxy_go_control_plane//pkg/cache/v3:cache",
        "@com_github_envoyproxy_go_control_plane//pkg/resource/v3:resource",
        "@com_github_envoyproxy_go_control_plane_envoy//config/cluster/v3:cluster",
        "@com_github_envoyproxy_go_control_plane_envoy//config/core/v3:core",
        "@com_github_envoyproxy_go_control_plane_envoy//config/endpoint/v3:endpoint",
        "@com_github_envoyproxy_go_control_plane_envoy//config/listener/v3:listener",
        "@com_github_envoyproxy_go_control_plane_envoy//config/route/v3:route",
        "@com_github_envoyproxy_go_control_plane_envoy//extensions/filters/network/tcp_proxy/v3:tcp_proxy",
        "@org_golang_google_protobuf//types/known/anypb",
        "@org_golang_google_protobuf//types/known/durationpb",
        "@org_golang_google_protobuf//types/known/wrapperspb",
    ],
)

go_test(
    name = "snapshot_test",
    srcs = ["desired_test.go"],
    embed = [":snapshot"],
    deps = [
        "@com_github_envoyproxy_go_control_plane//pkg/resource/v3:resource",
        "@com_github_envoyproxy_go_control_plane_envoy//config/cluster/v3:cluster",
        "@com_github_envoyproxy_go_control_plane_envoy//config/core/v3:core",
        "@com_github_envoyproxy_go_control_plane_envoy//config/endpoint/v3:endpoint",
        "@com_github_envoyproxy_go_control_plane_envoy//config/listener/v3:listener",
        "@com_github_envoyproxy_go_control_plane_envoy//config/route/v3:route",
        "@com_github_envoyproxy_go_control_plane_envoy//extensions/filters/network/tcp_proxy/v3:tcp_proxy",
    ],
)
