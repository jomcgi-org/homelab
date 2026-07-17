load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "agent-run_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/bazel/tools/agent-run",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_spf13_cobra//:go_default_library",
        "@io_k8s_api//core/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:go_default_library",
        "@io_k8s_apimachinery//pkg/apis/meta/v1/unstructured:go_default_library",
        "@io_k8s_apimachinery//pkg/runtime/schema:go_default_library",
        "@io_k8s_client_go//dynamic:go_default_library",
        "@io_k8s_client_go//kubernetes:go_default_library",
        "@io_k8s_client_go//kubernetes/scheme:go_default_library",
        "@io_k8s_client_go//rest:go_default_library",
        "@io_k8s_client_go//tools/clientcmd:go_default_library",
        "@io_k8s_client_go//tools/remotecommand:go_default_library",
        "@io_k8s_client_go//util/exec:go_default_library",
    ],
)

go_binary(
    name = "agent-run",
    embed = [":agent-run_lib"],
    visibility = ["//visibility:public"],
)

go_test(
    name = "agent-run_test",
    srcs = ["main_test.go"],
    embed = [":agent-run_lib"],
    deps = [
        "@com_github_stretchr_testify//assert:go_default_library",
        "@com_github_stretchr_testify//require:go_default_library",
        "@io_k8s_apimachinery//pkg/runtime/schema:go_default_library",
    ],
)
