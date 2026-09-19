"""Convert CRD OpenAPI schemas to kubeconform JSON schema files."""

import copy
import json
import pathlib
import sys

import yaml


def _close_objects(value, *, root=False):
    if isinstance(value, list):
        return [_close_objects(item) for item in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        if key in {"allOf", "anyOf", "oneOf"} and isinstance(item, list):
            # Constraint branches are layered over the enclosing schema. Their
            # properties are predicates, not the branch's complete object
            # shape, so closing the branch root rejects valid sibling fields.
            result[key] = [_close_objects(branch, root=True) for branch in item]
        else:
            result[key] = _close_objects(item)
    # Keep objects closed even beside x-kubernetes-preserve-unknown-fields:
    # this is stricter than the API server and matches openapi2jsonschema.
    if not root and "properties" in result and "additionalProperties" not in result:
        result["additionalProperties"] = False
    return result


def _replace_int_or_string(value):
    if isinstance(value, list):
        return [_replace_int_or_string(item) for item in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        if isinstance(item, dict) and item.get("format") == "int-or-string":
            result[key] = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        else:
            result[key] = _replace_int_or_string(item)
    return result


def _crds(document):
    if not isinstance(document, dict):
        return []
    if document.get("kind") == "List":
        return [item for item in document.get("items", []) if isinstance(item, dict)]
    if document.get("kind") == "CustomResourceDefinition":
        return [document]
    return []


def _schemas(crd):
    spec = crd.get("spec", {})
    kind = spec.get("names", {}).get("kind")
    group = spec.get("group")
    if not kind or not group:
        return

    versions = spec.get("versions") or [{"name": spec.get("version")}]
    for version in versions:
        version_name = version.get("name")
        schema = version.get("schema", {}).get("openAPIV3Schema")
        if schema is None:
            schema = spec.get("validation", {}).get("openAPIV3Schema")
        if version_name and schema:
            yield group, kind, version_name, schema


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: crd-schemas.py OUTPUT_DIR YAML...")

    output_dir = pathlib.Path(sys.argv[1])
    written = {}
    for source_name in sys.argv[2:]:
        source = pathlib.Path(source_name)
        with source.open(encoding="utf-8") as stream:
            for document in yaml.safe_load_all(stream):
                for crd in _crds(document):
                    for group, kind, version, schema in _schemas(crd):
                        converted = _replace_int_or_string(
                            _close_objects(copy.deepcopy(schema), root=True)
                        )
                        destination = (
                            output_dir / group / f"{kind.lower()}_{version}.json"
                        )
                        encoded = json.dumps(converted, indent=2, sort_keys=True) + "\n"
                        prior = written.get(destination)
                        if prior is not None and prior != encoded:
                            raise SystemExit(
                                f"conflicting schemas for {group}/{version}/{kind}: {source}"
                            )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_text(encoded, encoding="utf-8")
                        written[destination] = encoded

    if not written:
        raise SystemExit("no CustomResourceDefinition schemas found")
    print(f"Extracted {len(written)} CRD schemas into {output_dir}")


if __name__ == "__main__":
    main()
