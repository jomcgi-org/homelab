load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "agent-run_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/bazel/tools/agent-run",
    visibility = ["//visibility:private"],
    deps = [
        "@com_github_spf13_cobra//:cobra",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/apis/meta/v1/unstructured",
        "@io_k8s_apimachinery//pkg/runtime/schema",
        "@io_k8s_client_go//dynamic",
        "@io_k8s_client_go//kubernetes",
        "@io_k8s_client_go//kubernetes/scheme",
        "@io_k8s_client_go//rest",
        "@io_k8s_client_go//tools/clientcmd",
        "@io_k8s_client_go//tools/remotecommand",
        "@io_k8s_client_go//util/exec",
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
        "@com_github_stretchr_testify//assert",
        "@com_github_stretchr_testify//require",
        "@io_k8s_apimachinery//pkg/runtime/schema",
    ],
)
