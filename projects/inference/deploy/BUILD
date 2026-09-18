# gazelle:argocd disabled
# The ArgoCD Gazelle extension would generate render targets here that depend
# on the sibling chart target. The Helm extension resets that target's
# visibility on each format pass, so those generated targets cannot analyze.
# Hand-written home and GKE render targets live in ../chart/BUILD instead.

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/inference/chart:__pkg__"],
)
