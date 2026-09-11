load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = [
        "broker_transport.go",
        "classify.go",
        "main.go",
        "quota.go",
        "swap.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/firecracker/substrate/egress-proxy/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_spiffe_go_spiffe_v2//bundle/x509bundle",
        "@com_github_spiffe_go_spiffe_v2//spiffeid",
        "@com_github_spiffe_go_spiffe_v2//spiffetls/tlsconfig",
        "@com_github_spiffe_go_spiffe_v2//svid/x509svid",
        "@com_github_spiffe_go_spiffe_v2//workloadapi",
    ],
)

go_binary(
    name = "cmd",
    embed = [":cmd_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "cmd_test",
    srcs = [
        "broker_transport_test.go",
        "classify_test.go",
        "main_test.go",
        "quota_test.go",
        "swap_test.go",
    ],
    embed = [":cmd_lib"],
    deps = [
        "@com_github_spiffe_go_spiffe_v2//bundle/x509bundle",
        "@com_github_spiffe_go_spiffe_v2//spiffeid",
        "@com_github_spiffe_go_spiffe_v2//spiffetls/tlsconfig",
        "@com_github_spiffe_go_spiffe_v2//svid/x509svid",
    ],
)
