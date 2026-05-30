load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "keda",
    chart = "projects/platform/keda",
    chart_files = ":chart",
    namespace = "keda",
    release_name = "keda",
    # KEDA is secure-by-default (runAsNonRoot, readOnlyRootFilesystem, drops ALL
    # caps, seccomp RuntimeDefault) but its components do not mount a /tmp
    # emptyDir — the Go binaries do not write to /tmp, and the upstream chart
    # exposes no wrapper value to add one without custom extraVolumes plumbing.
    semgrep_exclude_rules = [
        "require-tmp-emptydir",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
