load("//bazel/helm:defs.bzl", "helm_chart")

# Render-only admission fixture. There is no publication or ArgoCD Application.
helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//visibility:public"],
)
