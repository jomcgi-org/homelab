load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_library(
    name = "egress-scan_lib",
    srcs = [
        "main.go",
        "rules.go",
        "scan.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_envoyproxy_go_control_plane_envoy//service/auth/v3:auth",
        "@com_github_envoyproxy_go_control_plane_envoy//type/v3:type",
        "@com_github_spf13_viper//:viper",
        "@com_github_zricethezav_gitleaks_v8//config",
        "@com_github_zricethezav_gitleaks_v8//logging",
        "@org_golang_google_genproto_googleapis_rpc//status",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//health",
        "@org_golang_google_grpc//health/grpc_health_v1",
    ],
)

go_binary(
    name = "egress-scan",
    embed = [":egress-scan_lib"],
    pure = "on",
    static = "on",
    visibility = ["//visibility:public"],
)

go_image(
    name = "image",
    binary = ":egress-scan",
)

go_test(
    name = "egress-scan_test",
    srcs = ["scan_test.go"],
    embed = [":egress-scan_lib"],
    deps = [
        "@com_github_envoyproxy_go_control_plane_envoy//service/auth/v3:auth",
        "@com_github_envoyproxy_go_control_plane_envoy//type/v3:type",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//credentials/insecure",
    ],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
