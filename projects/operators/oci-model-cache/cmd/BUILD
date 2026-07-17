load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")

go_library(
    name = "cmd_lib",
    srcs = ["main.go"],
    importpath = "github.com/jomcgi/homelab/projects/operators/oci-model-cache/cmd",
    visibility = ["//visibility:private"],
    deps = [
        "//bazel/tools/hf2oci/pkg/copy",
        "//bazel/tools/hf2oci/pkg/hf",
        "//projects/operators/oci-model-cache/api/v1alpha1",
        "//projects/operators/oci-model-cache/internal/config",
        "//projects/operators/oci-model-cache/internal/controller",
        "//projects/operators/oci-model-cache/internal/statemachine",
        "//projects/operators/oci-model-cache/internal/telemetry",
        "//projects/operators/oci-model-cache/internal/webhook",
        "@io_k8s_apimachinery//pkg/runtime:go_default_library",
        "@io_k8s_apimachinery//pkg/util/runtime:go_default_library",
        "@io_k8s_client_go//kubernetes/scheme:go_default_library",
        "@io_k8s_client_go//plugin/pkg/client/auth:go_default_library",
        "@io_k8s_sigs_controller_runtime//:go_default_library",
        "@io_k8s_sigs_controller_runtime//pkg/certwatcher:go_default_library",
        "@io_k8s_sigs_controller_runtime//pkg/healthz:go_default_library",
        "@io_k8s_sigs_controller_runtime//pkg/log/zap:go_default_library",
        "@io_k8s_sigs_controller_runtime//pkg/metrics/filters:go_default_library",
        "@io_k8s_sigs_controller_runtime//pkg/metrics/server:go_default_library",
        "@io_k8s_sigs_controller_runtime//pkg/webhook:go_default_library",
        "@io_k8s_sigs_controller_runtime//pkg/webhook/admission:go_default_library",
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
        "main_test.go",
        "resolver_adapter_test.go",
        "resolver_test.go",
    ],
    embed = [":cmd_lib"],
    deps = [
        "//bazel/tools/hf2oci/pkg/copy",
        "//bazel/tools/hf2oci/pkg/hf",
        "//projects/operators/oci-model-cache/internal/controller",
        "@com_github_google_go_containerregistry//pkg/registry:go_default_library",
    ],
)
