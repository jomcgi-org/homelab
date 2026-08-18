"""Bazel test rules for validating Helm charts."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")

def helm_template_test(name, chart, release_name, namespace, values_files, chart_files, **kwargs):
    """Creates a cacheable test that validates Helm chart renders with given values.

    This test runs helm template with the full values hierarchy from an ArgoCD
    Application and fails if rendering produces errors. Results are cached by
    Bazel based on input file hashes.

    Args:
        name: Name of the test target
        chart: Path to chart directory (e.g., "charts/todo")
        release_name: Helm release name
        namespace: Kubernetes namespace for rendering
        values_files: List of values file labels in order (e.g., ["//charts/todo:values.yaml", "values.yaml"])
        chart_files: Label for chart's all_files filegroup (e.g., "//charts/todo:all_files")
        **kwargs: Additional arguments passed to sh_test
    """
    sh_test(
        name = name,
        srcs = ["//bazel/helm:helm-template-test.sh"],
        args = [
            "$(rootpath @multitool//tools/helm)",
            release_name,
            chart,
            namespace,
        ] + ["$(rootpath {})".format(vf) for vf in values_files],
        data = [
            "@multitool//tools/helm",
            chart_files,
        ] + values_files,
        **kwargs
    )

def helm_annotation_test(name, chart, chart_files, release_name, namespace, annotations, set = [], **kwargs):
    """Creates a test that renders a Helm chart and asserts pod template annotations are present.

    This test renders the chart with helm template and checks that specific
    key=value annotations appear in the pod template metadata. Useful for
    asserting Linkerd, sidecar, or other required annotations are set by default.

    Args:
        name: Name of the test target
        chart: Path to chart directory (e.g., "projects/cluster_agents/deploy")
        chart_files: Label for chart's filegroup (e.g., ":chart")
        release_name: Helm release name
        namespace: Kubernetes namespace for rendering
        annotations: List of "KEY:VALUE" strings to assert in the rendered output
        set: Optional list of "K=V" strings forwarded as --set flags to helm template,
             allowing the chart to be rendered with non-default values
             (e.g., ["imagePullSecret.enabled=true", "priorityClassName=system-cluster-critical"]).
        **kwargs: Additional arguments passed to sh_test
    """

    # Build interleaved --set K=V args from the set list
    set_args = []
    for kv in set:
        set_args += ["--set", kv]

    sh_test(
        name = name,
        srcs = ["//bazel/helm:helm-assert-annotations.sh"],
        args = [
            "$(rootpath @multitool//tools/helm)",
            release_name,
            chart,
            namespace,
        ] + set_args + annotations,
        data = [
            "@multitool//tools/helm",
            chart_files,
        ],
        **kwargs
    )

def helm_lint_test(name, chart_path = None, extra_values = [], **kwargs):
    """Creates a test that runs helm lint on a chart.

    The test runs helm lint with --strict mode to catch any issues.

    Args:
        name: Name of the test target
        chart_path: Path to chart directory (default: current package)
        extra_values: Optional list of additional values file labels to pass
                      to helm lint via -f flags (e.g. generated image values).
        **kwargs: Additional arguments passed to sh_test
    """
    if chart_path == None:
        chart_path = native.package_name()

    # Create an inline script that runs helm lint.
    # Args: HELM CHART_YAML [EXTRA_VALUES_FILE ...]
    # Any arguments beyond the first two are passed as -f <file> to helm lint.
    native.genrule(
        name = name + "_script",
        outs = [name + ".sh"],
        cmd = """cat > $@ << 'EOF'
#!/usr/bin/env bash
set -euo pipefail

# Find helm binary
HELM="$$1"

# Chart.yaml path - get directory containing it
CHART_YAML="$$2"
CHART_DIR="$$(dirname "$$CHART_YAML")"

if [[ ! -f "$$CHART_YAML" ]]; then
    echo "ERROR: Chart.yaml not found at $$CHART_YAML"
    exit 1
fi

# Build extra -f flags from remaining arguments
EXTRA_VALUES=()
shift 2
for f in "$$@"; do
    EXTRA_VALUES+=(-f "$$f")
done

echo "Linting chart: $$CHART_DIR"
"$$HELM" lint "$$CHART_DIR" --strict "$${EXTRA_VALUES[@]+"$${EXTRA_VALUES[@]}"}"
echo "PASSED"
EOF
""",
    )

    sh_test(
        name = name,
        srcs = [name + "_script"],
        args = [
            "$(rootpath @multitool//tools/helm)",
            "$(rootpath :Chart.yaml)",
        ] + ["$(rootpath {})".format(f) for f in extra_values],
        data = [
                   "@multitool//tools/helm",
                   ":Chart.yaml",
                   ":values.yaml",
               ] + native.glob(["templates/**"], allow_empty = True) +
               # Vendored subcharts. Without these a chart that declares a
               # file:// dependency and calls {{ include }} on one of its
               # defines fails lint inside the sandbox with "no template
               # ... associated with template gotpl", because the tarball
               # never reached the runfiles tree. Every consumer of
               # //projects/platform/cf-ingress-library hit that and set
               # lint = False rather than fixing it here; those can be
               # turned back on now, one chart at a time.
               #
               # Additive: charts with no charts/ directory glob to nothing.
               native.glob(["charts/**"], allow_empty = True) + extra_values,
        **kwargs
    )
