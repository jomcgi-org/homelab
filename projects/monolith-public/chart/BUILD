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

# Guard (#5142): the destination-scoped egress policies must stay scoped. The
# test lives in //projects/monolith for the same reason as the HTTPRoute guard.
filegroup(
    name = "cilium_scoped_egress_template",
    srcs = ["templates/cilium-policy-scoped-egress.yaml"],
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

# Guard: the packaged homelab-library must deploy images by content digest, not
# by the build-timestamped tag (push-changed.sh skips content-identical images,
# so that tag is often never pushed: see homelab.imageRef). This exposes the
# PACKAGED tarball rather than the library source on purpose, because the
# tarball is what Bazel globs and ships; a source fix that was never re-synced
# into the tarball would deploy the old template while looking correct in the
# diff. The assertion lives in the Python-native //projects/monolith package.
filegroup(
    name = "packaged_library_chart",
    srcs = glob(["charts/homelab-library-*.tgz"]),
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
