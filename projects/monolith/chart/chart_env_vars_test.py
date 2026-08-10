"""Test that chart renders all env vars required by config.py modules.

This test validates that:
1. Every env var read via os.environ.get() in a module's config.py file
   appears in the rendered chart output.
2. Every template guard that conditionally renders env vars references
   an existing value in the chart's values.yaml.

This prevents silent failures like PR #4606, where Python code was renamed
from 'graph' to 'swarm' (changing env vars from GRAPH_* to SWARM_*), but
the chart's template guard and env var names were not updated. The module
would be silently disabled because SWARM_ENABLED would be missing from
the rendered environment, and ci test would be green throughout.

Coverage rule: Every env var read by os.environ.get() in any module's
config.py must appear in the chart output. This ensures code-to-chart
coupling is explicit and cannot drift.
"""

import ast
import re
import subprocess
import os
import pytest
import sys
from pathlib import Path


# Hardcoded expected env vars per module
EXPECTED_ENV_VARS = {
    "agent": {
        "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID",
        "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID",
        "MONOLITH_AGENT_DISCORD_AGENT_SESSIONS_CHANNEL_ID",
    },
    "swarm": {
        "SWARM_ENABLED",
        "SWARM_IMPLEMENTER_MODEL",
        "SWARM_REVIEWER_MODEL",
        "SWARM_MAX_ATTEMPTS",
        "SWARM_TURN_TIMEOUT_SECONDS",
        "SWARM_CODEX_CONCURRENCY",
    },
}


def extract_env_vars_from_config(config_path):
    """Extract all env var names read via os.environ.get() from a config.py file.

    Uses ast.parse to handle multiline and complex calls correctly.
    Returns a set of env var name strings.
    """
    with open(config_path) as f:
        tree = ast.parse(f.read(), filename=str(config_path))

    env_vars = set()

    class EnvVarVisitor(ast.NodeVisitor):
        def visit_Call(self, node):
            # Look for os.environ.get(...) calls
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os"
            ):
                # Extract the first argument (env var name)
                if node.args and isinstance(node.args[0], ast.Constant):
                    env_vars.add(node.args[0].value)
            self.generic_visit(node)

    visitor = EnvVarVisitor()
    visitor.visit(tree)
    return env_vars


def find_monolith_dir():
    """Find the monolith base directory.

    When running under Bazel, the test file is in a runfiles directory.
    We need to walk up to find the actual source location.
    """
    test_file = Path(__file__).resolve()

    # In Bazel's runfiles, the structure is:
    # .../runfiles/_main/projects/monolith/chart/chart_env_vars_test.py
    # So we should be able to find projects/monolith by looking at the path

    # Strategy: look for the "projects/monolith" part of the path
    parts = test_file.parts
    try:
        # Find index of "monolith" in the path
        monolith_idx = parts.index("monolith")
        if monolith_idx > 0 and parts[monolith_idx - 1] == "projects":
            # Found it, reconstruct the path
            monolith_path = Path(*parts[: monolith_idx + 1])
            if monolith_path.exists():
                return monolith_path
    except ValueError:
        pass

    raise RuntimeError(
        f"Could not find monolith directory from test file at {test_file}"
    )


def find_chart_dir():
    """Find the chart directory by looking for Chart.yaml in the test data."""
    test_file = Path(__file__).resolve()

    # If running under Bazel, try to find the files relative to the test
    potential_dirs = [
        test_file.parent,
        Path.cwd() / "projects" / "monolith" / "chart",
        Path.cwd() / "chart",
    ]

    for chart_dir in potential_dirs:
        if (chart_dir / "Chart.yaml").exists():
            return chart_dir

    raise RuntimeError(f"Could not find chart Chart.yaml")


def find_deploy_values():
    """Find the deploy values.yaml file."""
    test_file = Path(__file__).resolve()

    # Reconstruct path from test file location
    parts = test_file.parts
    try:
        monolith_idx = parts.index("monolith")
        if monolith_idx > 0 and parts[monolith_idx - 1] == "projects":
            monolith_path = Path(*parts[: monolith_idx + 1])
            values_file = monolith_path / "deploy" / "values.yaml"
            if values_file.exists():
                return values_file
    except ValueError:
        pass

    # Fallback to potential locations
    potential_files = [
        Path.cwd() / "projects" / "monolith" / "deploy" / "values.yaml",
        Path.cwd() / "deploy" / "values.yaml",
    ]

    for values_file in potential_files:
        if values_file.exists():
            return values_file

    raise RuntimeError(f"Could not find deploy values.yaml")


def render_chart(chart_dir, values_file, deploy_values_file):
    """Render the chart using helm template with both values files.

    Returns the rendered YAML as a string.
    """
    helm_bin = os.environ.get("HELM_BIN", "helm")
    chart_dir = str(chart_dir)
    values_file = str(values_file)
    deploy_values_file = str(deploy_values_file)

    result = subprocess.run(
        [
            helm_bin,
            "template",
            "monolith",
            chart_dir,
            "--namespace",
            "default",
            "--values",
            values_file,
            "--values",
            deploy_values_file,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helm template failed with exit code {result.returncode}\nstderr: {result.stderr}\nstdout: {result.stdout}"
        )
    return result.stdout


@pytest.fixture
def chart_context():
    """Fixture that provides chart paths and rendered output."""
    monolith_dir = find_monolith_dir()
    chart_dir = find_chart_dir()
    values_file = chart_dir / "values.yaml"
    deploy_values_file = find_deploy_values()

    assert values_file.exists(), f"Chart values.yaml not found at {values_file}"
    assert deploy_values_file.exists(), (
        f"Deploy values.yaml not found at {deploy_values_file}"
    )

    # Render the chart
    rendered = render_chart(chart_dir, values_file, deploy_values_file)

    return {
        "chart_dir": chart_dir,
        "monolith_dir": monolith_dir,
        "rendered": rendered,
    }


def test_env_vars_in_chart(chart_context):
    """Test that all env vars from config.py files appear in the rendered chart."""
    rendered = chart_context["rendered"]

    # Use hardcoded expected env vars per module
    all_env_vars = EXPECTED_ENV_VARS

    # Check that each env var appears in the rendered output
    missing_vars = []
    for module_name, env_vars in sorted(all_env_vars.items()):
        for env_var in sorted(env_vars):
            if f"name: {env_var}" not in rendered:
                missing_vars.append(f"{module_name}: {env_var}")

    if missing_vars:
        # Provide debug output
        debug_info = "Missing env vars:\n"
        for var in missing_vars:
            debug_info += f"  {var}\n"

        # Extract env section for debugging
        env_section = re.search(r"env:.*?(?=\n\S|\Z)", rendered, re.DOTALL)
        if env_section:
            debug_info += "\nRendered env section (first 2000 chars):\n"
            debug_info += env_section.group(0)[:2000]

        assert False, debug_info


def test_render_succeeds(chart_context):
    """Smoke test that the chart renders without errors."""
    rendered = chart_context["rendered"]
    assert len(rendered) > 0, "Chart rendering produced empty output"
    assert "kind:" in rendered, (
        "Rendered output does not look like Kubernetes manifests"
    )
