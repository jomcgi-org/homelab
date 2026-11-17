load("@rules_go//go:def.bzl", "go_test")

go_test(
    name = "admission_test",
    size = "large",
    srcs = ["admission_test.go"],
    data = [
        "//projects/factory-public/chart",
        "//projects/factory-public/chart:Chart.yaml",
        "@envtest_1_37_0_linux_amd64//:assets",
        "@envtest_1_37_0_linux_amd64//:kube-apiserver",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "MIRROR_CHART": "$(rootpath //projects/factory-public/chart:Chart.yaml)",
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
