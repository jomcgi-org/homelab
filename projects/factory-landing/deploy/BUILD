load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "factory-landing",
    chart = "projects/factory-landing/chart",
    chart_files = "//projects/factory-landing/chart:chart",
    content_rules = True,  # keep
    content_rules_exceptions = {
        # The landing page reads the connector status from the factory
        # namespace. Moving both into the factory chart removes this entry.
        "CiliumNetworkPolicy/factory/factory-landing-status": "lets the landing page reach the connector status port",
        "Service/factory/factory-connector-status": "exposes the connector status port to the landing page",
    },  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "factory-landing",
    release_name = "factory-landing",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/factory-landing/chart:values.yaml",
        "//projects/factory-landing/chart:image_values",
    ],  # keep
)
