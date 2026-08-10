"""Guard authentik blueprint YAML tags so malformed !Env cannot break discovery.

authentik's !Env tag constructor reads node.value[1] with no length check:
  if isinstance(node, SequenceNode):
    self.key = loader.construct_object(node.value[0])
    self.default = loader.construct_object(node.value[1])   # IndexError on a 1-element list

Valid forms are:
  - !Env KEY (scalar form)
  - !Env [KEY, default] (2-element sequence form)

Invalid form that raises IndexError:
  - !Env [KEY] (1-element sequence form)

The blast radius: authentik's blueprints_find() catches only YAMLError, so an
IndexError escapes and aborts blueprints_discovery for the ENTIRE /blueprints
tree. One malformed tag in one file silently stops every blueprint in the
instance from reconciling, including authentik's own bundled defaults.

This test guards against that by:
1. Discovering all blueprints/*.yaml files and parsing them
2. Building YAML AST without constructing objects, so we can inspect node shapes
3. Asserting that any !Env in sequence form has exactly 2 elements
4. Failing with a message naming the offending file and line/column if found
"""

import pathlib
import pytest
import yaml


def _assert_env_arity_node(node, label):
    """Recursively assert !Env arity in a node tree.

    Checks that any SequenceNode with tag '!Env' has exactly 2 elements.
    Leaves other custom tags alone (their arity requirements are unconfirmed).
    See issue #4620 for extending this to guard other tags.
    """
    if node is None:
        return

    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            _assert_env_arity_node(key, label)
            _assert_env_arity_node(value, label)
    elif isinstance(node, yaml.SequenceNode):
        # Check if this node has the !Env tag
        if node.tag == "!Env":
            if len(node.value) != 2:
                mark = node.start_mark
                raise AssertionError(
                    f"{label}: !Env sequence form must have exactly 2 elements, "
                    f"got {len(node.value)} at line {mark.line + 1}, column {mark.column + 1}"
                )
        # Recurse into children
        for child in node.value:
            _assert_env_arity_node(child, label)
    elif isinstance(node, yaml.ScalarNode):
        pass  # Scalars have no children


def _parse_and_check_blueprint(yaml_text, label):
    """Parse YAML AST and assert !Env arity.

    Args:
        yaml_text: YAML content as a string
        label: human-readable label for error messages (e.g., filename)

    Raises:
        AssertionError if an !Env sequence form has != 2 elements
        yaml.YAMLError if the YAML is malformed
    """
    try:
        # Build the AST using compose_all; this builds nodes without calling
        # constructors, so we can inspect the raw tag and structure.
        for event in yaml.compose_all(yaml_text, Loader=yaml.SafeLoader):
            _assert_env_arity_node(event, label)
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"{label}: {e}") from e


# Real blueprint files
BLUEPRINT_DIR = pathlib.Path(__file__).resolve().parent / "blueprints"


def _blueprint_files():
    """Discover all YAML blueprint files."""
    if BLUEPRINT_DIR.exists():
        return sorted(BLUEPRINT_DIR.glob("*.yaml"))
    return []


@pytest.mark.parametrize("blueprint_file", _blueprint_files(), ids=lambda p: p.name)
def test_blueprint_yaml_arity(blueprint_file):
    """Assert all blueprint YAML files have valid !Env arity."""
    content = blueprint_file.read_text()
    _parse_and_check_blueprint(content, blueprint_file.name)


# Planted tests: valid and invalid forms of !Env


def test_env_scalar_form_valid():
    """Scalar form !Env KEY is always valid."""
    yaml_text = """
version: 1
metadata:
  secret: !Env MY_SECRET
"""
    _parse_and_check_blueprint(yaml_text, "test_env_scalar_form_valid")


def test_env_sequence_form_with_default_valid():
    """Sequence form !Env [KEY, default] with 2 elements is valid."""
    yaml_text = """
version: 1
metadata:
  secret: !Env [MY_SECRET, "default-value"]
"""
    _parse_and_check_blueprint(yaml_text, "test_env_sequence_form_with_default_valid")


def test_env_sequence_form_missing_default_invalid():
    """Sequence form !Env [KEY] with 1 element is invalid (IndexError in authentik)."""
    yaml_text = """
version: 1
metadata:
  secret: !Env [MY_SECRET]
"""
    with pytest.raises(AssertionError) as exc_info:
        _parse_and_check_blueprint(
            yaml_text, "test_env_sequence_form_missing_default_invalid"
        )

    # Verify the error message names the issue
    assert "!Env sequence form must have exactly 2 elements" in str(exc_info.value)
    assert "got 1" in str(exc_info.value)


def test_env_sequence_form_too_many_elements_invalid():
    """Sequence form !Env [K, default, extra] with 3+ elements is invalid."""
    yaml_text = """
version: 1
metadata:
  secret: !Env [MY_SECRET, "default", "extra"]
"""
    with pytest.raises(AssertionError) as exc_info:
        _parse_and_check_blueprint(
            yaml_text, "test_env_sequence_form_too_many_elements_invalid"
        )

    # Verify the error message names the issue
    assert "!Env sequence form must have exactly 2 elements" in str(exc_info.value)
    assert "got 3" in str(exc_info.value)
