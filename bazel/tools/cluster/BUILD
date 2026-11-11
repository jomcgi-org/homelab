load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

"""
Session-authenticated ArgoCD API helper.

Cluster mutations must go through Git + ArgoCD.
"""

# Session-authenticated ArgoCD API calls (port-forward + admin JWT exchange).
# Still read-only against the cluster: it only calls the ArgoCD API with
# whatever curl args the caller passes; it does not itself mutate anything.
sh_binary(
    name = "argocd_api",
    srcs = ["scripts/argocd-api.sh"],
)
