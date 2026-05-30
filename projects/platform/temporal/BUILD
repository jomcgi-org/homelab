load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "temporal",
    chart = "projects/platform/temporal",
    chart_files = ":chart",
    namespace = "temporal",
    release_name = "temporal",
    # Upstream temporal chart hard-codes container specs that its values do not
    # expose knobs for. We genuinely harden every container the chart DOES let us
    # configure (runAsNonRoot + drop ALL + allowPrivilegeEscalation:false on
    # server/web/admintools/schema-job via *.containerSecurityContext); the
    # exclusions below cover only the upstream-uncontrollable remainder:
    #   - require-tmp-emptydir: chart authors no /tmp emptyDir and exposes no
    #     per-container volumeMount for the schema-job `done`/`create-namespace`
    #     and `helm test` pod containers.
    #   - writable-filesystem-container: server/UI/admin-tools/sql-tool images
    #     write to their root FS (dockerize config render, work dirs); the chart
    #     exposes no readOnlyRootFilesystem + writable-mount pairing per container.
    #   - run-as-non-root: the `cluster-health` pod in templates/test.yaml (a
    #     `helm.sh/hook: test` resource ArgoCD never deploys) is hard-coded with
    #     no securityContext and no enable/disable or securityContext value.
    #   - require-resource-limits / require-readiness-probe: not configurable for
    #     all subcomponents.
    semgrep_exclude_rules = [
        "require-tmp-emptydir",
        "require-resource-limits",
        "require-readiness-probe",
        "yaml.kubernetes.security.writable-filesystem-container.writable-filesystem-container",
        "yaml.kubernetes.security.run-as-non-root.run-as-non-root",
    ],
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
