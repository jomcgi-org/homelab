"""Renders Helm manifests for the chart admissibility gate (#4831).

The platform charts get their render_manifests genrule from argocd_app. The
first-party applications (embervm, monolith, monolith-public, inference,
context-forge-gateway, oci-model-cache-operator, signoz-alerts) commit their
ArgoCD Applications directly and have no argocd_app call, so they use this
macro to produce the same public manifests/all.yaml output that
//bazel/helm:chart_admissibility_test validates with kubeconform.
"""

def helm_admissibility_render(name, chart, chart_files, release_name, namespace, values_files = [], visibility = ["//visibility:public"]):
    """Creates a genrule that renders a chart with its real value stack.

    The output feeds //bazel/helm:chart_admissibility_test, which pipes it
    through kubeconform -strict. Keep the helm invocation identical to what
    ArgoCD does at sync time: release name from the Application metadata.name,
    namespace from spec.destination.namespace, values files in application.yaml
    order.

    Args:
        name: Base name for the genrule ("render_manifests" at every call site).
        chart: Workspace-relative path to the chart directory.
        chart_files: Label for the chart's filegroup (e.g. "//projects/monolith/chart:chart").
        release_name: Helm release name (Application metadata.name).
        namespace: Kubernetes namespace (spec.destination.namespace).
        values_files: Values file labels in application.yaml order. Relative
            names resolve inside the calling package; absolute labels must be
            single-file targets whose target name matches the file name, so the
            workspace-relative path can be derived from the label.
        visibility: Visibility for the rendered output; the admissibility test
            in //bazel/helm must be able to read it.
    """
    cmd_parts = [
        "$(location @multitool//tools/helm)",
        "template",
        release_name,
        chart,
        "--namespace",
        namespace,
    ]

    # Convert labels to workspace-relative paths the same way app.bzl does:
    # "//pkg:file" -> "pkg/file", "values.yaml" -> "<package>/values.yaml".
    pkg = native.package_name()
    for vf in values_files:
        if vf.startswith("//"):
            label_path = vf[2:]
            vpkg, _, vfile = label_path.partition(":")
            if vfile:
                cmd_parts.extend(["--values", vpkg + "/" + vfile])
            else:
                cmd_parts.extend(["--values", vpkg])
        else:
            cmd_parts.extend(["--values", pkg + "/" + vf])

    srcs = [chart_files] + list(values_files)

    # Deduplicate while preserving order, normalizing relative labels so a
    # colocated chart's "values.yaml" is recognized once.
    seen = {}
    deduped = []
    for s in srcs:
        key = s
        if not s.startswith("//") and not s.startswith("@") and not s.startswith(":"):
            key = "//" + pkg + ":" + s
        elif s.startswith(":"):
            key = "//" + pkg + s
        if key not in seen:
            seen[key] = True
            deduped.append(s)

    native.genrule(
        name = name,
        srcs = deduped,
        outs = ["manifests/all.yaml"],
        cmd = " ".join(cmd_parts) + " > $@",
        tools = ["@multitool//tools/helm"],
        local = True,
        tags = ["manual"],
        visibility = visibility,
    )
