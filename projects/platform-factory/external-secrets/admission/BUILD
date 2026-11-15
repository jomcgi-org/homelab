load("@rules_go//go:def.bzl", "go_test")

go_test(
    name = "admission_test",
    size = "large",
    srcs = ["admission_test.go"],
    data = [
        "//projects/platform-factory/external-secrets:application.yaml",
        "//projects/platform-factory/external-secrets:checks/allowed.yaml",
        "//projects/platform-factory/external-secrets:checks/denied.yaml",
        "//projects/platform-factory/external-secrets:values.yaml",
        "//projects/platform-factory/external-secrets/chart:Chart.yaml",
        "//projects/platform-factory/external-secrets/chart:chart",
        "@envtest_1_37_0_linux_amd64//:assets",
        "@envtest_1_37_0_linux_amd64//:kube-apiserver",
        "@external_secrets_chart_2_15_0//:Chart.yaml",
        "@external_secrets_chart_2_15_0//:chart_files",
        "@multitool//tools/helm",
    ],
    env = {
        "ESO_ALLOWED": "$(rootpath //projects/platform-factory/external-secrets:checks/allowed.yaml)",
        "ESO_APPLICATION": "$(rootpath //projects/platform-factory/external-secrets:application.yaml)",
        "ESO_DENIED": "$(rootpath //projects/platform-factory/external-secrets:checks/denied.yaml)",
        "ESO_GUARDS": "$(rootpath //projects/platform-factory/external-secrets/chart:Chart.yaml)",
        "ESO_UPSTREAM": "$(rootpath @external_secrets_chart_2_15_0//:Chart.yaml)",
        "ESO_VALUES": "$(rootpath //projects/platform-factory/external-secrets:values.yaml)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "TEST_APISERVER": "$(rootpath @envtest_1_37_0_linux_amd64//:kube-apiserver)",
    },
    deps = [
        "@io_k8s_apimachinery//pkg/api/errors",
        "@io_k8s_apimachinery//pkg/apis/meta/v1:meta",
        "@io_k8s_apimachinery//pkg/apis/meta/v1/unstructured",
        "@io_k8s_apimachinery//pkg/runtime/schema",
        "@io_k8s_apimachinery//pkg/util/yaml",
        "@io_k8s_client_go//dynamic",
        "@io_k8s_sigs_controller_runtime//pkg/envtest",
    ],
)
