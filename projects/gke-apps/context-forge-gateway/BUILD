# gazelle:argocd disabled
# This package exports the hub policy for Loom's Application contract test.
# The existing deploy package owns gateway rendering and admissibility; a
# duplicate argocd_app here cannot access its private chart/value inputs.
exports_files(
    ["application.yaml"],
    visibility = ["//projects/loom/deploy:__pkg__"],
)
