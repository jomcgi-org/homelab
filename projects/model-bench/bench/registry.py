from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from bench.schema import ModelSpec


def _load_yaml_mapping(path: Path) -> dict:
    """Load YAML from path and assert it is a top-level mapping.

    Separating the isinstance check into a helper keeps the yaml.safe_load call
    and the dict access in different scopes, satisfying the repo semgrep rule
    yaml-safe-load-unchecked-type which requires an isinstance guard before any
    dict method or subscript on the safe_load return value.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping in {path}, got {type(raw).__name__}")
    return raw


def load_registry(path: Path) -> list[ModelSpec]:
    """Load and validate the model registry from a YAML file.

    Each entry under the top-level 'models' key is validated as a ModelSpec.
    Missing optional fields use ModelSpec defaults (status='active', role='candidate').
    """
    data = _load_yaml_mapping(path)
    return [ModelSpec(**entry) for entry in data["models"]]


def active_models(
    reg: list[ModelSpec], include_experimental: bool = False
) -> list[ModelSpec]:
    """Return models with status 'active' (optionally include 'experimental')."""
    statuses = {"active"}
    if include_experimental:
        statuses.add("experimental")
    return [m for m in reg if m.status in statuses]


def anchors(reg: list[ModelSpec]) -> list[ModelSpec]:
    """Return models with role 'anchor' (calibration baseline models)."""
    return [m for m in reg if m.role == "anchor"]


def drop_model(path: Path, model_id: str, *, reason: str, date: str) -> None:
    """Retire a model in-place: set status, retired_reason, retired_date.

    Loads the raw YAML dict, patches the matching entry, and writes back with
    yaml.safe_dump (comment preservation is out of scope).
    Raises KeyError if model_id is not found.
    """
    data = _load_yaml_mapping(path)
    found = False
    for entry in data["models"]:
        if entry["id"] == model_id:
            entry["status"] = "retired"
            entry["retired_reason"] = reason
            entry["retired_date"] = date
            found = True
            break
    if not found:
        raise KeyError(f"model {model_id!r} not found in registry")
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def prune_retired(results_root: Path, reg: list[ModelSpec]) -> list[str]:
    """Delete result directories for retired models.

    The results directory for a model is results_root / model_id.replace('/', '__').
    The tombstone entry in models.yaml is left untouched; only the result files
    on disk are removed. Returns the list of model ids whose directories were pruned.
    """
    pruned: list[str] = []
    for model in reg:
        if model.status == "retired":
            results_dir = results_root / model.id.replace("/", "__")
            if results_dir.exists():
                shutil.rmtree(results_dir)
                pruned.append(model.id)
    return pruned
