"""Macro for declaring ArgoCD application overlays."""

load("//bazel/helm:test.bzl", "helm_template_test")

def argocd_app(
        name,
        chart,
        chart_files,
        release_name = None,
        namespace = "default",
        values_files = [],
        generate_manifests = True,
        tags = [],
        target_suffix = ""):
    """Declares an ArgoCD application overlay with template testing and manifest rendering.

    This macro replaces the genrule + helm_template_test pattern that Gazelle
    previously generated for each overlay directory. It creates:
    - A helm_template_test (always) to validate chart rendering
    - A render_manifests genrule (opt-in, default True) for pre-rendering

    Args:
        name: Base name for generated targets
        chart: Path to chart directory (e.g., "charts/todo")
        chart_files: Label for chart's filegroup (e.g., "//charts/todo:chart")
        release_name: Helm release name (defaults to name)
        namespace: Kubernetes namespace (default: "default")
        values_files: List of values file labels in order
        generate_manifests: If True, create render_manifests genrule (default: True)
        tags: Additional tags for the template test
        target_suffix: Suffix for template and manifest targets sharing a package
    """
    if release_name == None:
        release_name = name

    # Always create a template test. target_suffix keeps a second overlay in
    # the same package (e.g. a values-gke.yaml variant) from colliding with
    # the default target names.
    helm_template_test(
        name = "template_test" + target_suffix,
        chart = chart,
        chart_files = chart_files,
        release_name = release_name,
        namespace = namespace,
        values_files = values_files,
        tags = tags + ["helm", "template"],
    )

    if generate_manifests:
        # Build the helm template command matching what generate.go produces
        cmd_parts = [
            "$(location @multitool//tools/helm)",
            "template",
            release_name,
            chart,
            "--namespace",
            namespace,
            "--include-crds",
        ]

        # Add values files as --values flags using workspace-relative paths
        for vf in values_files:
            # Convert Bazel labels to workspace-relative paths for the local genrule
            # "//charts/todo:values.yaml" -> "charts/todo/values.yaml"
            # "values.yaml" (local) -> "<current_package>/values.yaml"
            if vf.startswith("//"):
                # Absolute label: //pkg:file -> pkg/file
                label_path = vf[2:]  # strip //
                pkg, _, file = label_path.partition(":")
                if file:
                    cmd_parts.extend(["--values", pkg + "/" + file])
                else:
                    cmd_parts.extend(["--values", pkg])
            else:
                # Relative label: use package path
                cmd_parts.extend(["--values", native.package_name() + "/" + vf])

        cmd_parts.extend([">", "$@"])

        # Collect the chart and values inputs. application.yaml is not used by
        # helm template and may live in a different package for GKE overlays.
        chart_label = "//" + chart
        srcs_list = [chart_files, chart_label + ":Chart.yaml", chart_label + ":values.yaml"] + values_files

        # Deduplicate while preserving order, normalizing labels to absolute
        # form so that "values.yaml" and "//pkg:values.yaml" are recognized as
        # the same file when chart and overlay are colocated.
        pkg = native.package_name()
        seen = {}
        srcs = []
        for s in srcs_list:
            key = s
            if not s.startswith("//") and not s.startswith("@") and not s.startswith(":"):
                key = "//" + pkg + ":" + s
            elif s.startswith(":"):
                key = "//" + pkg + s
            if key not in seen:
                seen[key] = True
                srcs.append(s)

        native.genrule(
            name = "render_manifests" + target_suffix,
            srcs = srcs,
            outs = ["manifests" + target_suffix + "/all.yaml"],
            cmd = " ".join(cmd_parts),
            tools = [
                "//bazel/helm:render-manifests.sh",
                "@multitool//tools/helm",
            ],
            local = True,
            tags = ["manual"],
            visibility = ["//visibility:public"],
        )
