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
        "@io_k8s_apimachinery//pkg/runtime",
        "@io_k8s_apimachinery//pkg/util/runtime",
        "@io_k8s_client_go//kubernetes/scheme",
        "@io_k8s_client_go//plugin/pkg/client/auth",
        "@io_k8s_sigs_controller_runtime//:controller-runtime",
        "@io_k8s_sigs_controller_runtime//pkg/certwatcher",
        "@io_k8s_sigs_controller_runtime//pkg/healthz",
        "@io_k8s_sigs_controller_runtime//pkg/log/zap",
        "@io_k8s_sigs_controller_runtime//pkg/metrics/filters",
        "@io_k8s_sigs_controller_runtime//pkg/metrics/server",
        "@io_k8s_sigs_controller_runtime//pkg/webhook",
        "@io_k8s_sigs_controller_runtime//pkg/webhook/admission",
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
        "@com_github_google_go_containerregistry//pkg/registry",
    ],
)
