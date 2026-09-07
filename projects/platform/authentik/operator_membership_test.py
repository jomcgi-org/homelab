"""Guard the operators group entitlement and akadmin membership ownership.

The operators group gates the standing human factory and intervention guards,
which match the literal group name. This test pins the source-only contract:

1. Exactly one authentik_core.group declares stable id/name operators with
   attrs.is_superuser false.
2. The akadmin user entry in mcp-auth.yaml is the ONLY writer of the intended
   human's memberships and must keep granting operators plus the three prior
   references (authentik Admins, homelab-admin, family). Extra future grants
   are allowed, so the check is a subset check, never an exact list match.
3. No other user (kg-agent-sa or anyone else) is granted operators.
4. No second active blueprint writes akadmin memberships from the user side,
   and no group entry writes them from the group side (attrs.users), since
   either would flap on reconcile.

Like blueprint_tags_test.py this parses YAML to an AST with compose_all and
never constructs !Env/!Find/!KeyOf values and never reads the environment.
It scans every active blueprints/*.yaml file so competing ownership in another
file is caught.
"""

import pathlib

import pytest
import yaml

INTENDED_USER = "akadmin"
SERVICE_USER = "kg-agent-sa"
OPERATORS = "operators"
SERVICE_GROUP = "kg-agents"
REQUIRED_GRANTS = ("authentik Admins", "homelab-admin", "family", OPERATORS)


def _scalar(node):
    if isinstance(node, yaml.ScalarNode):
        return node.value
    return None


def _map_get(mapping, key):
    if not isinstance(mapping, yaml.MappingNode):
        return None
    for k, v in mapping.value:
        if _scalar(k) == key:
            return v
    return None


def _resolve_group_ref(node):
    """Resolve a user attrs.groups item to a group name, or None if unknown."""
    if isinstance(node, yaml.ScalarNode):
        if node.tag in ("tag:yaml.org,2002:str", "!KeyOf"):
            return node.value
        return None
    if isinstance(node, yaml.SequenceNode) and node.tag == "!Find":
        if len(node.value) != 2:
            return None
        model = _scalar(node.value[0])
        pair = node.value[1]
        if model != "authentik_core.group":
            return None
        if isinstance(pair, yaml.SequenceNode) and len(pair.value) == 2:
            field = _scalar(pair.value[0])
            name = _scalar(pair.value[1])
            if field == "name":
                return name
        return None
    return None


def _resolve_user_ref(node):
    """Resolve a group attrs.users item to a username, or None if unknown."""
    if isinstance(node, yaml.ScalarNode):
        return node.value
    if isinstance(node, yaml.SequenceNode) and node.tag == "!Find":
        if len(node.value) != 2:
            return None
        model = _scalar(node.value[0])
        pair = node.value[1]
        if model != "authentik_core.user":
            return None
        if isinstance(pair, yaml.SequenceNode) and len(pair.value) == 2:
            field = _scalar(pair.value[0])
            name = _scalar(pair.value[1])
            if field in ("username", "id"):
                return name
        return None
    return None


def _collect(text):
    """Collect group and user entries from blueprint YAML text as plain data."""
    groups = []
    users = []
    for doc in yaml.compose_all(text, Loader=yaml.SafeLoader):
        if not isinstance(doc, yaml.MappingNode):
            continue
        entries = _map_get(doc, "entries")
        if not isinstance(entries, yaml.SequenceNode):
            continue
        for entry in entries.value:
            if not isinstance(entry, yaml.MappingNode):
                continue
            model = _scalar(_map_get(entry, "model"))
            attrs = _map_get(entry, "attrs")
            identifiers = _map_get(entry, "identifiers")
            if model == "authentik_core.group":
                name = _scalar(_map_get(identifiers, "name"))
                entry_id = _scalar(_map_get(entry, "id"))
                is_superuser = _scalar(_map_get(attrs, "is_superuser"))
                users_node = _map_get(attrs, "users")
                user_refs = []
                if isinstance(users_node, yaml.SequenceNode):
                    user_refs = [_resolve_user_ref(i) for i in users_node.value]
                groups.append(
                    {
                        "id": entry_id,
                        "name": name,
                        "is_superuser": is_superuser,
                        "user_refs": user_refs,
                        "declares_users": isinstance(users_node, yaml.SequenceNode),
                    }
                )
            elif model == "authentik_core.user":
                username = _scalar(_map_get(identifiers, "username"))
                groups_node = _map_get(attrs, "groups")
                grants = []
                if isinstance(groups_node, yaml.SequenceNode):
                    grants = [_resolve_group_ref(i) for i in groups_node.value]
                users.append(
                    {
                        "username": username,
                        "grants": grants,
                        "writes_membership": isinstance(groups_node, yaml.SequenceNode),
                    }
                )
    return groups, users


def _validate(label_to_text):
    """Validate the operators entitlement across the given blueprint texts."""
    groups = []
    users = []
    for label, text in label_to_text.items():
        try:
            file_groups, file_users = _collect(text)
        except yaml.YAMLError as e:
            raise AssertionError(f"{label}: unparseable blueprint YAML: {e}") from e
        for g in file_groups:
            g["label"] = label
        for u in file_users:
            u["label"] = label
        groups.extend(file_groups)
        users.extend(file_users)

    operators_groups = [
        g for g in groups if g["id"] == OPERATORS or g["name"] == OPERATORS
    ]
    if len(operators_groups) != 1:
        raise AssertionError(
            "expected exactly one operators group, found "
            f"{len(operators_groups)}: "
            + ", ".join(g["label"] for g in operators_groups)
        )
    operators_group = operators_groups[0]
    if operators_group["id"] != OPERATORS or operators_group["name"] != OPERATORS:
        raise AssertionError(
            f"{operators_group['label']}: operators group must have stable "
            f"id/name operators, got id={operators_group['id']!r} "
            f"name={operators_group['name']!r}"
        )
    if operators_group["is_superuser"] != "false":
        raise AssertionError(
            f"{operators_group['label']}: operators group must declare "
            "attrs.is_superuser false, granting superuser would entitle "
            "every operator beyond the intended standing human scope"
        )

    writers = [
        u for u in users if u["username"] == INTENDED_USER and u["writes_membership"]
    ]
    if len(writers) != 1:
        raise AssertionError(
            f"expected exactly one writer of {INTENDED_USER} memberships, found "
            f"{len(writers)}: " + ", ".join(u["label"] for u in writers)
        )
    grants = set(writers[0]["grants"])
    for required in REQUIRED_GRANTS:
        if required not in grants:
            raise AssertionError(
                f"{writers[0]['label']}: {INTENDED_USER} memberships must keep "
                f"granting {required!r}"
            )

    for u in users:
        if u["username"] == INTENDED_USER:
            continue
        if OPERATORS in (u["grants"] or []):
            raise AssertionError(
                f"{u['label']}: user {u['username']!r} must not be granted "
                "operators, only the intended human holds it"
            )

    for u in users:
        if u["username"] == SERVICE_USER and u["writes_membership"]:
            if SERVICE_GROUP not in (u["grants"] or []):
                raise AssertionError(
                    f"{u['label']}: {SERVICE_USER} must stay in {SERVICE_GROUP}"
                )

    granted = {gname for gname in grants if gname is not None}
    for g in groups:
        if g["declares_users"]:
            if g["id"] == OPERATORS or g["name"] == OPERATORS:
                raise AssertionError(
                    f"{g['label']}: operators group must not declare users, "
                    f"{INTENDED_USER} memberships are owned by the user side only"
                )
            if INTENDED_USER in (g["user_refs"] or []):
                raise AssertionError(
                    f"{g['label']}: group {g['name']!r} must not declare "
                    f"{INTENDED_USER} in users, that membership has exactly "
                    "one writer on the user side"
                )
            if g["id"] in granted or g["name"] in granted:
                raise AssertionError(
                    f"{g['label']}: group {g['name']!r} must not declare "
                    "users, the intended human holds a grant there and the "
                    "user side owns that membership (a replacement list "
                    "removes the human even when it omits them)"
                )


BLUEPRINT_DIR = pathlib.Path(__file__).resolve().parent / "blueprints"


def _blueprint_files():
    if BLUEPRINT_DIR.exists():
        return sorted(BLUEPRINT_DIR.glob("*.yaml"))
    return []


def _label_to_text():
    return {p.name: p.read_text() for p in _blueprint_files()}


def test_operator_blueprints_are_discovered():
    """Missing runfile data must fail, never silently pass with zero files."""
    assert BLUEPRINT_DIR.is_dir(), f"blueprint dir missing: {BLUEPRINT_DIR}"
    found = sorted(BLUEPRINT_DIR.glob("*.yaml"))
    assert len(found) >= 2, f"expected at least 2 blueprints, found {len(found)}"


def test_operators_entitlement_across_blueprints():
    _validate(_label_to_text())


def _wrap(entries_body):
    return "version: 1\nentries:\n" + entries_body


_OPERATORS_GROUP = """\
- model: authentik_core.group
  id: operators
  identifiers:
    name: operators
  attrs:
    is_superuser: false
"""

_AKADMIN_FULL = """\
- model: authentik_core.user
  identifiers:
    username: akadmin
  attrs:
    groups:
      - !Find [authentik_core.group, [name, authentik Admins]]
      - !KeyOf homelab-admin
      - !KeyOf operators
      - !Find [authentik_core.group, [name, family]]
"""

_KG_AGENT_SA = """\
- model: authentik_core.user
  id: kg-agent-sa
  identifiers:
    username: kg-agent-sa
  attrs:
    groups:
      - !KeyOf kg-agents
"""

_VALID_DOC = _wrap(_OPERATORS_GROUP + _AKADMIN_FULL + _KG_AGENT_SA)


def test_valid_entitlement_passes():
    _validate({"valid.yaml": _VALID_DOC})


def test_missing_operators_membership_invalid():
    doc = _wrap(
        _OPERATORS_GROUP
        + """\
- model: authentik_core.user
  identifiers:
    username: akadmin
  attrs:
    groups:
      - !Find [authentik_core.group, [name, authentik Admins]]
      - !KeyOf homelab-admin
      - !Find [authentik_core.group, [name, family]]
"""
        + _KG_AGENT_SA
    )
    with pytest.raises(AssertionError, match="operators"):
        _validate({"missing-operators.yaml": doc})


def test_missing_family_membership_invalid():
    doc = _wrap(
        _OPERATORS_GROUP
        + """\
- model: authentik_core.user
  identifiers:
    username: akadmin
  attrs:
    groups:
      - !Find [authentik_core.group, [name, authentik Admins]]
      - !KeyOf homelab-admin
      - !KeyOf operators
"""
        + _KG_AGENT_SA
    )
    with pytest.raises(AssertionError, match="family"):
        _validate({"missing-family.yaml": doc})


def test_service_account_operators_grant_invalid():
    doc = _wrap(
        _OPERATORS_GROUP
        + _AKADMIN_FULL
        + """\
- model: authentik_core.user
  id: kg-agent-sa
  identifiers:
    username: kg-agent-sa
  attrs:
    groups:
      - !KeyOf kg-agents
      - !KeyOf operators
"""
    )
    with pytest.raises(AssertionError, match="kg-agent-sa"):
        _validate({"service-account-grant.yaml": doc})


def test_other_user_operators_grant_invalid():
    doc = _wrap(
        _OPERATORS_GROUP
        + _AKADMIN_FULL
        + _KG_AGENT_SA
        + """\
- model: authentik_core.user
  identifiers:
    username: someone-else
  attrs:
    groups:
      - !KeyOf operators
"""
    )
    with pytest.raises(AssertionError, match="someone-else"):
        _validate({"other-user-grant.yaml": doc})


def test_duplicate_intended_user_writer_invalid():
    doc_a = _wrap(_OPERATORS_GROUP + _AKADMIN_FULL + _KG_AGENT_SA)
    doc_b = _wrap(
        """\
- model: authentik_core.user
  identifiers:
    username: akadmin
  attrs:
    groups:
      - !KeyOf operators
"""
    )
    with pytest.raises(AssertionError, match="exactly one writer"):
        _validate({"first.yaml": doc_a, "second.yaml": doc_b})


def test_group_side_membership_writer_invalid():
    doc = _wrap(
        """\
- model: authentik_core.group
  id: operators
  identifiers:
    name: operators
  attrs:
    is_superuser: false
    users:
      - !Find [authentik_core.user, [username, akadmin]]
"""
        + _AKADMIN_FULL
        + _KG_AGENT_SA
    )
    with pytest.raises(AssertionError, match="must not declare"):
        _validate({"group-side-writer.yaml": doc})


def test_operators_group_must_not_be_superuser():
    doc = _wrap(
        """\
- model: authentik_core.group
  id: operators
  identifiers:
    name: operators
  attrs:
    is_superuser: true
"""
        + _AKADMIN_FULL
        + _KG_AGENT_SA
    )
    with pytest.raises(AssertionError, match="is_superuser"):
        _validate({"superuser.yaml": doc})


def test_duplicate_operators_group_invalid():
    doc = _wrap(_OPERATORS_GROUP + _OPERATORS_GROUP + _AKADMIN_FULL + _KG_AGENT_SA)
    with pytest.raises(AssertionError, match="exactly one operators group"):
        _validate({"duplicate-group.yaml": doc})


def test_granted_group_empty_users_list_invalid():
    """An empty group side users list still replaces (removes) membership."""
    moving_doc = _wrap(
        """\
- model: authentik_core.group
  identifiers:
    name: family
  attrs:
    users: []
"""
    )
    with pytest.raises(AssertionError, match="must not declare"):
        _validate({"valid.yaml": _VALID_DOC, "moving-auth.yaml": moving_doc})


def test_granted_group_other_user_only_invalid():
    """A group side list with only another user still removes the human."""
    moving_doc = _wrap(
        """\
- model: authentik_core.group
  identifiers:
    name: family
  attrs:
    users:
      - !Find [authentik_core.user, [username, someone-else]]
"""
    )
    with pytest.raises(AssertionError, match="must not declare"):
        _validate({"valid.yaml": _VALID_DOC, "moving-auth.yaml": moving_doc})


def test_unrelated_group_users_list_allowed():
    """Groups without a grant for the intended human may declare users."""
    extra_doc = _wrap(
        """\
- model: authentik_core.group
  id: some-club
  identifiers:
    name: some-club
  attrs:
    is_superuser: false
    users:
      - !Find [authentik_core.user, [username, someone-else]]
"""
    )
    _validate({"valid.yaml": _VALID_DOC, "extra.yaml": extra_doc})


def test_future_human_memberships_are_preserved():
    """Unknown extra grants on the intended human must keep passing."""
    doc = _wrap(
        _OPERATORS_GROUP
        + """\
- model: authentik_core.user
  identifiers:
    username: akadmin
  attrs:
    groups:
      - !Find [authentik_core.group, [name, authentik Admins]]
      - !KeyOf homelab-admin
      - !KeyOf operators
      - !Find [authentik_core.group, [name, family]]
      - !Find [authentik_core.group, [name, some-future-group]]
"""
        + _KG_AGENT_SA
    )
    _validate({"future-grant.yaml": doc})
