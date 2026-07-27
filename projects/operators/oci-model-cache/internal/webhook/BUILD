load("@rules_go//go:def.bzl", "go_library", "go_test")

go_library(
    name = "webhook",
    srcs = ["pod_mutator.go"],
    importpath = "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/webhook",
    visibility = ["//projects/operators/oci-model-cache:__subpackages__"],
    deps = [
        "//bazel/tools/hf2oci/pkg/hf",
        "//bazel/tools/hf2oci/pkg/ociref",
        "//projects/operators/oci-model-cache/api/v1alpha1",
        "//projects/operators/oci-model-cache/internal/controller",
        "//projects/operators/oci-model-cache/internal/hfref",
        "//projects/operators/oci-model-cache/internal/naming",
        "//projects/operators/oci-model-cache/internal/statemachine",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_sigs_controller_runtime//pkg/client",
        "@io_k8s_sigs_controller_runtime//pkg/log",
        "@io_k8s_sigs_controller_runtime//pkg/webhook/admission",
    ],
)

go_test(
    name = "webhook_test",
    srcs = [
        "pod_mutator_coverage_test.go",
        "pod_mutator_test.go",
    ],
    embed = [":webhook"],
    deps = [
        "//bazel/tools/hf2oci/pkg/hf",
        "//projects/operators/oci-model-cache/api/v1alpha1",
        "//projects/operators/oci-model-cache/internal/statemachine",
        "@com_github_stretchr_testify//assert",
        "@com_github_stretchr_testify//require",
        "@io_k8s_api//admission/v1:admission",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/runtime",
        "@io_k8s_apimachinery//pkg/runtime/schema",
        "@io_k8s_sigs_controller_runtime//pkg/client",
        "@io_k8s_sigs_controller_runtime//pkg/client/fake",
        "@io_k8s_sigs_controller_runtime//pkg/client/interceptor",
        "@io_k8s_sigs_controller_runtime//pkg/webhook/admission",
    ],
)
