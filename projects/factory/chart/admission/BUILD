load("@rules_go//go:def.bzl", "go_test")

go_test(
    name = "admission_test",
    size = "large",
    srcs = ["admission_test.go"],
    data = [
        "//projects/factory/chart",
        "//projects/factory/chart:Chart.yaml",
        "//projects/factory/deploy:deploy_values",
        "@envtest_1_37_0_linux_amd64//:assets",
        "@envtest_1_37_0_linux_amd64//:kube-apiserver",
        "@multitool//tools/helm",
    ],
    env = {
        "DEPLOY_VALUES": "$(rootpath //projects/factory/deploy:deploy_values)",
        "FACTORY_CHART": "$(rootpath //projects/factory/chart:Chart.yaml)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "TEST_APISERVER": "$(rootpath @envtest_1_37_0_linux_amd64//:kube-apiserver)",
    },
    deps = [
        "//projects/factory/connector/runbookadmission",
        "@io_k8s_api//admissionregistration/v1:admissionregistration",
        "@io_k8s_api//authorization/v1:authorization",
        "@io_k8s_api//core/v1:core",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/apis/meta/v1/unstructured",
        "@io_k8s_apimachinery//pkg/runtime/schema",
        "@io_k8s_apimachinery//pkg/util/yaml",
        "@io_k8s_client_go//dynamic",
        "@io_k8s_client_go//kubernetes",
        "@io_k8s_client_go//rest",
        "@io_k8s_sigs_controller_runtime//pkg/envtest",
    ],
)
