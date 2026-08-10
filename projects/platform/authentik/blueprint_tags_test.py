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

Note the asymmetry in why each rejected form is rejected, because the two are
NOT the same kind of problem:

  - 1 element is authentik's actual contract. It raises IndexError and takes
    down discovery. This is the bug the guard exists for.
  - 3 or more elements is a LINT, not authentik's contract. The constructor
    reads only value[0] and value[1], so extra elements are silently ignored
    and discovery is unaffected. We still reject it, because a third element
    is almost certainly an authoring mistake and silence is the worst outcome,
    but do not read the failure message as "authentik rejects this". It does
    not.

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
        # A mapping-form !Env matches neither isinstance branch in authentik's
        # constructor, so `key` is never set and it does NOT break discovery: it
        # surfaces much later as an AttributeError when the blueprint is applied.
        # Catching it here moves that from apply time to CI, which is the whole
        # point of this guard.
        if node.tag == "!Env":
            mark = node.start_mark
            raise AssertionError(
                f"{label}: !Env must be a scalar or a 2-element sequence, got a "
                f"mapping at line {mark.line + 1}, column {mark.column + 1}"
            )
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


def test_blueprints_are_discovered():
    """Fail loudly if the data glob stops materialising the blueprints.

    Without this, a broken `data` dep in BUILD would leave _blueprint_files()
    empty, parametrize would generate zero cases, and the file-driven guard
    below would vanish while the suite still reported green. An empty
    parametrize list is a silent pass, which is the one outcome a guard must
    never produce.
    """
    assert BLUEPRINT_DIR.is_dir(), f"blueprint dir missing: {BLUEPRINT_DIR}"
    found = _blueprint_files()
    assert len(found) >= 2, f"expected at least 2 blueprints, found {len(found)}"


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
    """Sequence form !Env [K, default, extra] is rejected as a LINT, not by authentik.

    authentik reads only value[0] and value[1], so a third element is silently
    ignored and discovery keeps working. We reject it anyway because it is
    almost certainly an authoring mistake, but unlike the 1-element case this
    is our rule, not authentik's.
    """
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


def test_env_mapping_form_invalid():
    """Mapping form !Env is rejected here rather than at apply time.

    It does not raise IndexError, so it does not break discovery the way the
    1-element sequence does. It fails later, when the blueprint is applied.
    """
    yaml_text = """
version: 1
metadata:
  secret: !Env
    key: MY_SECRET
    default: fallback
"""
    with pytest.raises(AssertionError) as exc_info:
        _parse_and_check_blueprint(yaml_text, "test_env_mapping_form_invalid")

    assert "must be a scalar or a 2-element sequence" in str(exc_info.value)
