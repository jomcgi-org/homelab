load("//bazel/helm:defs.bzl", "helm_chart")

helm_chart(
    name = "chart",
    images = {
        "controllerManager.image": "//projects/operators/oci-model-cache:image.info",
    },
    publish = True,
    # //overlays packages diff and promote this chart; the oci-model-cache
    # deploy package renders it for the chart_admissibility_test (#4831).
    visibility = [
        "//overlays:__subpackages__",
        "//projects/operators:__subpackages__",
    ],
)
