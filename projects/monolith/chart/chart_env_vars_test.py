"""Test that chart renders all env vars required by config.py modules.

This test validates that every env var referenced in any monolith module's
config.py file appears in the rendered Helm chart output. This prevents the
class of bug that occurred in PR #4606, where Python code was renamed from
'graph' to 'swarm' (changing env vars from GRAPH_* to SWARM_*), but the
chart template was not updated, causing silent module disablement.

The test discovers config.py files at runtime, parses them with ast.parse(),
extracts all os.environ accesses (both [] and .get()), and verifies each
var appears in the rendered chart. This ensures the test cannot go stale:
adding a new module with config.py is automatically caught.

Coverage rule: Every env var read via os.environ in a module's config.py
must be provisioned by the chart. This ensures code-to-chart coupling is
explicit. If a module legitimately relies on env vars not set by the chart
(e.g., from mounted Secrets), those should be read differently, not through
config.py. An opt-out set may be added if needed (see code below).
"""

import ast
import re
import subprocess
import os
import pytest
from pathlib import Path


def find_config_modules():
    """Discover all config.py files in the monolith directory tree.

    Returns a dict mapping module_name -> Path to config.py.
    Searches in the Bazel runfiles for projects/monolith/*/config.py files.
    """
    test_file = Path(__file__).resolve()

    # Try multiple strategies to find config.py files
    # Strategy 1: Search in parent dirs for monolith/ and then look for config.py files
    current = test_file.parent
    while current.parent != current:
        candidate = current / "agent" / "config.py"
        if candidate.exists():
            # Found monolith directory, scan for config.py files
            configs = {}
            for item in current.iterdir():
                if item.is_dir():
                    config_py = item / "config.py"
                    if config_py.exists():
                        configs[item.name] = config_py
            if configs:
                return configs
        current = current.parent

    raise RuntimeError(f"Could not find config.py modules from {test_file}")


def extract_env_vars_from_config(config_path):
    """Extract all env var names accessed in a config.py file.

    Uses ast.parse to handle both os.environ.get("NAME") and
    os.environ["NAME"] patterns. Returns a set of env var name strings.
    """
    with open(config_path) as f:
        tree = ast.parse(f.read(), filename=str(config_path))

    env_vars = set()

    class EnvVarVisitor(ast.NodeVisitor):
        def visit_Subscript(self, node):
            # Handle os.environ["NAME"] pattern
            if (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "os"
            ):
                if isinstance(node.slice, ast.Constant):
                    env_vars.add(node.slice.value)
            self.generic_visit(node)

        def visit_Call(self, node):
            # Handle os.environ.get("NAME", ...) pattern
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os"
            ):
                if node.args and isinstance(node.args[0], ast.Constant):
                    env_vars.add(node.args[0].value)
            self.generic_visit(node)

    visitor = EnvVarVisitor()
    visitor.visit(tree)
    return env_vars


def find_chart_dir():
    """Find the chart directory."""
    test_file = Path(__file__).resolve()
    if (test_file.parent / "Chart.yaml").exists():
        return test_file.parent
    raise RuntimeError("Could not find chart Chart.yaml")


def render_chart(chart_dir, values_file, deploy_values_file):
    """Render the chart using helm template with both values files."""
    helm_bin = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm_bin,
            "template",
            "monolith",
            str(chart_dir),
            "--namespace",
            "default",
            "--values",
            str(values_file),
            "--values",
            str(deploy_values_file),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helm template failed with exit code {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout


@pytest.fixture
def chart_context():
    """Fixture that provides chart paths and rendered output."""
    chart_dir = find_chart_dir()
    values_file = chart_dir / "values.yaml"
    deploy_values_file = os.environ.get("DEPLOY_VALUES")

    assert values_file.exists(), f"Chart values.yaml not found at {values_file}"
    assert deploy_values_file, "DEPLOY_VALUES environment variable not set"
    assert Path(deploy_values_file).exists(), (
        f"Deploy values.yaml not found at {deploy_values_file}"
    )

    rendered = render_chart(chart_dir, values_file, deploy_values_file)

    return {
        "chart_dir": chart_dir,
        "rendered": rendered,
    }


def test_env_vars_in_chart(chart_context):
    """Test that all env vars from discovered config.py files appear in chart."""
    rendered = chart_context["rendered"]

    # Discover config.py modules and extract expected env vars
    config_modules = find_config_modules()
    assert len(config_modules) > 0, "No config.py modules found"

    all_env_vars = {}
    for module_name, config_path in sorted(config_modules.items()):
        env_vars = extract_env_vars_from_config(config_path)
        if env_vars:
            all_env_vars[module_name] = env_vars

    assert len(all_env_vars) > 0, "No env vars extracted from any config.py"

    # Check that each env var appears in the rendered output
    missing_vars = []
    for module_name, env_vars in sorted(all_env_vars.items()):
        for env_var in sorted(env_vars):
            if f"name: {env_var}" not in rendered:
                missing_vars.append(f"{module_name}: {env_var}")

    if missing_vars:
        debug_info = "Missing env vars:\n"
        for var in missing_vars:
            debug_info += f"  {var}\n"
        env_section = re.search(r"env:.*?(?=\n\S|\Z)", rendered, re.DOTALL)
        if env_section:
            debug_info += "\nRendered env section (first 1500 chars):\n"
            debug_info += env_section.group(0)[:1500]
        assert False, debug_info


def test_migrations_configmap_uses_server_side_apply(chart_context):
    """The migrations ConfigMap must opt into server-side apply (#5150).

    Every chart/migrations/*.sql file is globbed into one ConfigMap. Under
    client-side apply, kubectl stores the whole object in the
    last-applied-configuration annotation, which the apiserver caps at
    262144 bytes; the live object sat at ~244 KiB on 2026-08-22. Server-side
    apply does not write that annotation, so the cap stops being a ceiling on
    migration history. The resource-level sync option keeps every other object
    on the Application's default apply mode.
    """
    rendered = chart_context["rendered"]
    docs = [
        d
        for d in rendered.split("\n---")
        if "-migrations\n" in d and "kind: ConfigMap" in d
    ]
    assert len(docs) == 1, (
        f"expected exactly one migrations ConfigMap, found {len(docs)}"
    )
    assert "argocd.argoproj.io/sync-options: ServerSideApply=true" in docs[0], (
        "migrations ConfigMap is missing the ServerSideApply=true sync option"
    )
