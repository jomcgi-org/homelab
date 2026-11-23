load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "gate_lib",
    srcs = [
        "ateclient.go",
        "config.go",
        "gate.go",
        "main.go",
        "metrics.go",
        "pool.go",
        "router.go",
        "tls.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate",
    visibility = ["//visibility:private"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/gate/ateapi",
        "@com_github_prometheus_client_golang//prometheus",
        "@com_github_prometheus_client_golang//prometheus/collectors",
        "@com_github_prometheus_client_golang//prometheus/promhttp",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//credentials",
        "@org_golang_google_grpc//metadata",
        "@org_golang_google_grpc//status",
    ],
)

go_binary(
    name = "gate",
    embed = [":gate_lib"],
    pure = "on",
    static = "on",
    visibility = ["//projects/platform-factory/substrate/egress-scan/gate/image:__pkg__"],
)

go_test(
    name = "gate_test",
    size = "small",
    srcs = [
        "bench_test.go",
        "fake_test.go",
        "gate_test.go",
        "pool_test.go",
        "tls_test.go",
    ],
    embed = [":gate_lib"],
    deps = [
        "//projects/platform-factory/substrate/egress-scan/gate/ateapi",
        "@com_github_prometheus_client_golang//prometheus/testutil",
        "@org_golang_google_grpc//:grpc",
        "@org_golang_google_grpc//codes",
        "@org_golang_google_grpc//credentials",
        "@org_golang_google_grpc//credentials/insecure",
        "@org_golang_google_grpc//metadata",
        "@org_golang_google_grpc//status",
        "@org_golang_google_grpc//test/bufconn",
        "@org_golang_google_protobuf//proto",
        "@org_golang_google_protobuf//types/known/timestamppb",
    ],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)
