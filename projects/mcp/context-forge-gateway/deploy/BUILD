# gazelle:argocd disabled
# The argocd gazelle extension would generate an argocd_app here (this is
# the one git-chart single-source app) whose template and semgrep tests
# depend on the chart target, visible only to //overlays; the extension
# also resets that visibility on every format pass, so the generated
# targets can never analyse. The chart renders are covered by the
# hand-written targets in ../chart/BUILD instead.

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/mcp/context-forge-gateway/chart:__pkg__"],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/mcp/context-forge-gateway/chart:__pkg__"],
)

