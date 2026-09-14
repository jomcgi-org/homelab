# gazelle:exclude reconcile_orphaned_iscsi.py
# gazelle:exclude reconcile_orphaned_iscsi_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "reconcile_orphaned_iscsi_lib",
    srcs = ["reconcile_orphaned_iscsi.py"],
    imports = ["."],
)

py_test(
    name = "reconcile_orphaned_iscsi_test",
    srcs = ["reconcile_orphaned_iscsi_test.py"],
    deps = [
        ":reconcile_orphaned_iscsi_lib",
        "@pip//pytest",
    ],
)

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "longhorn",
    chart = "projects/platform/longhorn",
    chart_files = ":chart",
    namespace = "longhorn",
    release_name = "longhorn",
    # Longhorn is a storage system that legitimately requires privileged containers; upstream chart does not expose resource limits for all manager containers; manager containers also lack readiness probes
    semgrep_exclude_rules = [
        "no-privileged",
        "require-resource-limits",
        "require-readiness-probe",
        "yaml.kubernetes.security.privileged-container.privileged-container",
        "yaml.kubernetes.security.allow-privilege-escalation.allow-privilege-escalation",
        "yaml.kubernetes.security.writable-filesystem-container.writable-filesystem-container",
        "yaml.kubernetes.security.run-as-non-root.run-as-non-root",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
    ],
)
