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
# + KEDA scalers (ADRs agents/015, platform/004). AUTHOR-ONLY this wavefront:
# CI-validated via helm_template_test / semgrep_manifest_test (see
# ../deploy:lakehouse) but NOT published or wired into ArgoCD yet.
#
# No `images=` pinning here: the dispatchers image is a sibling W4 unit not yet
# built in this branch, and helm_images_values requires every referenced
# image .info target to exist. The orchestrator adds image pinning when it wires
# the chart (worker -> //projects/lakehouse/image:image.info, quack ->
# //projects/lakehouse/quack-server:image.info, dispatchers -> its target).
helm_chart(
    name = "chart",
    lint = False,
    # //projects/lakehouse:__subpackages__ so the separate deploy/ package's
    # argocd_app (which references //projects/lakehouse/chart:chart) can see the
    # chart filegroup; //overlays for the historical overlay rendering.
    visibility = [
        "//overlays:__subpackages__",
        "//projects/lakehouse:__subpackages__",
    ],
)
