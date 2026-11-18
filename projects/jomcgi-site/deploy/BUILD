load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "jomcgi-site-dev",
    chart = "projects/jomcgi-site/chart",
    chart_files = "//projects/jomcgi-site/chart:chart",
    content_rules = True,  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "jomcgi-site-dev",
    release_name = "jomcgi-site-dev",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/jomcgi-site/chart:values.yaml",
        "//projects/jomcgi-site/chart:image_values",
    ],  # keep
)

genrule(
    name = "prod_values",
    srcs = ["application-prod.yaml"],
    outs = ["values-prod.yaml"],
    cmd = "$(location @multitool//tools/yq) -e '.spec.source.helm.valuesObject' $< > $@",
    tools = ["@multitool//tools/yq"],
)

# keep
argocd_app(
    name = "jomcgi-site-prod",
    chart = "projects/jomcgi-site/chart",
    chart_files = "//projects/jomcgi-site/chart:chart",
    content_rules = True,
    generate_manifests = False,
    namespace = "jomcgi-site-prod",
    target_suffix = "_prod",
    values_files = [
        "//projects/jomcgi-site/chart:values.yaml",
        "//projects/jomcgi-site/chart:image_values",
        ":prod_values",
    ],
)
