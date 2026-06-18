load("//bazel/helm:defs.bzl", "helm_chart")

# Guard (ADR 005): the public HTTPRoute must never route chat or the internal
# API. The test that enforces this lives in the Python-native //projects/monolith
# package (this is a Helm-chart package with no Python toolchain context); it
# consumes this template via the filegroup below.
filegroup(
    name = "httproute_public_template",
    srcs = ["templates/httproute-public.yaml"],
    visibility = ["//projects/monolith:__pkg__"],
)

# Guard (ADR 005): the Turnstile *secret* must stay in the backend ("web"); the
# frontend may reference only the public site-key literal. The test asserting
# this lives in the Python-native //projects/monolith package and reads this
# chart's values.yaml via the filegroup below.
filegroup(
    name = "chart_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith:__pkg__"],
)

helm_chart(
    name = "chart",
    images = {
        "web.image": "//projects/monolith:image_public.info",
        "frontend.image": "//projects/monolith/frontend:image_public.info",
    },
    lint = False,
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
