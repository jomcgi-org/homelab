"""Every application on the open-signup factory instance needs a group gate."""

from pathlib import Path

import yaml


class BlueprintLoader(yaml.SafeLoader):
    """Keep Authentik references as tagged nodes without evaluating them."""


BlueprintLoader.add_multi_constructor("!", lambda loader, tag, node: node)


def test_factory_applications_have_group_bindings():
    paths = sorted((Path(__file__).parent / "factory-blueprints").glob("*.yaml"))
    assert paths, "No factory blueprints discovered"
    missing = []
    for path in paths:
        entries = yaml.load(path.read_text(), Loader=BlueprintLoader)["entries"]
        bound = set()
        for entry in entries:
            if entry["model"] != "authentik_policies.policybinding":
                continue
            fields = {**entry.get("identifiers", {}), **entry.get("attrs", {})}
            target = fields.get("target")
            if (
                entry.get("state", "present") == "present"
                and fields.get("enabled", True) is True
                and fields.get("group")
                and isinstance(target, yaml.ScalarNode)
                and target.tag == "!KeyOf"
            ):
                bound.add(target.value)

        for entry in entries:
            if (
                entry["model"] == "authentik_core.application"
                and entry.get("id") not in bound
            ):
                missing.append(f"{path.name}: {entry['identifiers']['slug']}")

    assert not missing, "Missing enabled group binding for " + ", ".join(missing)
