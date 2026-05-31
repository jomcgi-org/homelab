load("//bazel/helm:defs.bzl", "helm_chart")

# gazelle:argocd disabled
#
# The helm gazelle extension regenerates `helm_chart` with visibility derived
# solely from overlays/ usage (generate.go discoverOverlaysUsingChart), which
# resets our deploy-package visibility. This app is path-based with a separate
# deploy/ package, so we hand-maintain both this BUILD and ../deploy/BUILD and
# tell gazelle to leave them alone.
#
# Helm chart for the event-sourced lakehouse worker/quack/dispatcher Deployments
# + KEDA scalers (ADRs agents/015, platform/004). Published as an OCI chart and
# wired into ArgoCD (../deploy:lakehouse); CI-validated via helm_template_test /
# semgrep_manifest_test.
#
# `images=` + `publish=True` make CI (a) deep-merge each image's CI-stamped tag
# into the packaged chart's values.yaml at build time (helm_images_values — the
# OCI chart ships digest-pinned; never hand-edit `@sha256:` into values) and
# (b) push the chart to ghcr.io/jomcgi/homelab/charts by its Chart.yaml version.
# All three image `.info` targets now exist (worker / quack-server / dispatchers).
# NOTE: ../deploy/values.yaml must NOT set `image.tag` or the git overlay clobbers
# the pin (Helm valueFiles are last-wins over the packaged base).
helm_chart(
    name = "chart",
    images = {
        "worker.image": "//projects/lakehouse/image:image.info",
        "quack.image": "//projects/lakehouse/quack-server:image.info",
        "dispatchers.image": "//projects/lakehouse/dispatchers:image.info",
    },
    lint = False,
    publish = True,
    # //projects/lakehouse:__subpackages__ so the separate deploy/ package's
    # argocd_app (which references //projects/lakehouse/chart:chart) can see the
    # chart filegroup; //overlays for the historical overlay rendering.
    visibility = [
        "//overlays:__subpackages__",
        "//projects/lakehouse:__subpackages__",
    ],
)
