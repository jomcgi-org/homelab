load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    # The vendored upstream subchart is linted by its own repo, not ours.
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "polylane",
    chart = "projects/platform/polylane",
    chart_files = ":chart",
    # The render genrule hard-codes application.yaml into its srcs, and this
    # Application lives in projects/platform-gke/polylane, so there is nothing
    # here for it to render. The semgrep scan reads the chart, not the
    # Application, so it stays on.
    generate_manifests = False,
    namespace = "polylane",
    release_name = "polylane-k8s",
    # cloudflared runs runAsNonRoot with a read-only root filesystem and
    # upstream gives it one mount, the state Secret. It writes nothing to /tmp,
    # so the emptyDir the rule asks for would be dead weight.
    semgrep_exclude_rules = ["require-tmp-emptydir"],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
