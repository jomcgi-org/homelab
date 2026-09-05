load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "tokenbroker_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/tokenbroker/cmd/tokenbroker",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/embervm/tokenbroker/internal/broker",
        "//projects/embervm/tokenbroker/internal/metrics",
        "//projects/embervm/tokenbroker/internal/provider",
        "//projects/embervm/tokenbroker/internal/provider/authentik",
        "//projects/embervm/tokenbroker/internal/provider/codexchatgpt",
        "//projects/embervm/tokenbroker/internal/quota",
        "//projects/embervm/tokenbroker/internal/store",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@com_github_spiffe_go_spiffe_v2//spiffeid",
        "@com_github_spiffe_go_spiffe_v2//spiffetls/tlsconfig",
        "@com_github_spiffe_go_spiffe_v2//svid/x509svid",
        "@com_github_spiffe_go_spiffe_v2//workloadapi",
        "@io_k8s_client_go//kubernetes",
        "@io_k8s_client_go//rest",
    ],
)

go_binary(
    name = "tokenbroker",
    embed = [":tokenbroker_lib"],
    visibility = ["//projects/embervm/tokenbroker:__subpackages__"],
)

go_test(
    name = "tokenbroker_test",
    srcs = [
        "main_test.go",
        "spiffe_test.go",
    ],
    embed = [":tokenbroker_lib"],
    deps = [
        "//projects/embervm/tokenbroker/internal/broker",
        "//projects/embervm/tokenbroker/internal/metrics",
        "//projects/embervm/tokenbroker/internal/provider",
        "//projects/embervm/tokenbroker/internal/quota",
        "//projects/embervm/tokenbroker/internal/store",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/testutil",
        "@com_github_spiffe_go_spiffe_v2//bundle/x509bundle",
        "@com_github_spiffe_go_spiffe_v2//spiffeid",
        "@com_github_spiffe_go_spiffe_v2//spiffetls/tlsconfig",
        "@com_github_spiffe_go_spiffe_v2//svid/x509svid",
        "@com_github_spiffe_go_spiffe_v2//workloadapi",
    ],
)
