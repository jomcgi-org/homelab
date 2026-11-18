load("//bazel/helm:defs.bzl", "argocd_app")

exports_files(["application.yaml"])

argocd_app(
    name = "factory-public",
    chart = "projects/factory-public/chart",
    chart_files = "//projects/factory-public/chart:chart",
    content_rules = True,  # keep
    content_rules_exceptions = {
        "Role/argocd/factory-public-health-apps": "The health publisher (HOME-260) reads the argocd and jomcgi-site-prod Applications, which live in argocd.",
        "RoleBinding/argocd/factory-public-health-apps": "Binds that read-only Role to the health ServiceAccount.",
    },
    namespace = "factory-public",
    release_name = "factory-public",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/factory-public/chart:values.yaml"],
)
