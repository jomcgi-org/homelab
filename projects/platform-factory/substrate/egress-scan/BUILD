load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_library(
    name = "egress-scan_lib",
    srcs = [
        "detectors.go",
        "labels.go",
        "main.go",
        "metrics.go",
        "scan.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/detect",
        "//projects/platform-factory/substrate/egress-scan/detect/betterleaks",
        "//projects/platform-factory/substrate/egress-scan/detect/gitleaks",
        "@com_github_envoyproxy_go_control_plane_envoy//service/auth/v3:auth",
        "@com_github_envoyproxy_go_control_plane_envoy//type/v3:type",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
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
    srcs = [
        "labels_test.go",
        "scan_test.go",
    ],
    embed = [":egress-scan_lib"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/detect",
        "@com_github_envoyproxy_go_control_plane_envoy//config/core/v3:core",
        "@com_github_envoyproxy_go_control_plane_envoy//service/auth/v3:auth",
        "@com_github_envoyproxy_go_control_plane_envoy//type/v3:type",
        "@com_github_prometheus_client_golang//prometheus",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//credentials/insecure",
        "@org_golang_google_protobuf//types/known/structpb",
    ],
)

filegroup(
    name = "repo_docs",
    srcs = [
        "ARCHITECTURE.md",
        "DESIGN-detectors.md",
        "DESIGN-large-bodies.md",
        "DESIGN-prior-art.md",
        "README.md",
    ],
    visibility = ["//projects/monolith:__pkg__"],
)
