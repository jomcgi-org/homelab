load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "onepassword",
    chart = "projects/platform/onepassword",
    chart_files = "//projects/platform/onepassword:chart",
    namespace = "onepassword",
    release_name = "connect",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/onepassword:values.yaml",
        "//projects/platform/onepassword:values-factory.yaml",
    ],
)

# Requires kubectl and the synced factory cluster.
sh_test(
    name = "admission_test",
    srcs = ["test-admission.sh"],
    tags = [
        "local",
        "manual",
    ],
)
