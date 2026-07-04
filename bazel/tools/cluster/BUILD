load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

"""
Read-only cluster inspection targets.

These targets wrap kubectl commands for inspecting cluster state.
All operations are strictly read-only -- mutations must go through Git + ArgoCD.
"""

sh_binary(
    name = "pods",
    srcs = ["scripts/pods.sh"],
)

sh_binary(
    name = "events",
    srcs = ["scripts/events.sh"],
)

sh_binary(
    name = "status",
    srcs = ["scripts/status.sh"],
)

sh_binary(
    name = "argocd",
    srcs = ["scripts/argocd.sh"],
)

# Session-authenticated ArgoCD API calls (port-forward + admin JWT exchange).
# Still read-only against the cluster: it only calls the ArgoCD API with
# whatever curl args the caller passes; it does not itself mutate anything.
sh_binary(
    name = "argocd_api",
    srcs = ["scripts/argocd-api.sh"],
)
