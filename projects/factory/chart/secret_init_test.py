"""secret_init.py's key-generation handoff to the api and worker: the ID is
stored with the keys, so a run that fails before it restarts the pods is
completed by the next run."""

from __future__ import annotations

import base64
import copy
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(
    os.environ.get(
        "SECRET_INIT",
        Path(__file__).resolve().parent / "files" / "itsaplan" / "secret_init.py",
    )
)
_ROLL = "factory.jomcgi.dev/itsaplan-key-generation"
_ALLOW = "factory.jomcgi.dev/regenerate-itsaplan-keys"
_DEPLOYMENTS = ["factory-itsaplan-api", "factory-itsaplan-worker"]


class FakeAPI:
    """Secrets and Deployments in one namespace, with a PATCH that can fail."""

    def __init__(self, secrets: dict[str, dict[str, Any]]) -> None:
        self.secrets = secrets
        self.templates = {name: {} for name in _DEPLOYMENTS}
        self.fail_patch = False
        self.puts: list[str] = []

    def call(self, method, path, body=None, content_type="application/json"):
        name = path.rsplit("/", 1)[1]
        if "/secrets/" in path:
            if method == "GET":
                return copy.deepcopy(self.secrets[name])
            assert method == "PUT"
            self.secrets[name] = copy.deepcopy(body)
            self.puts.append(name)
            return body
        assert method == "PATCH" and content_type == "application/merge-patch+json"
        if self.fail_patch:
            raise SystemExit(f"PATCH {path}: 500")
        annotations = body["spec"]["template"]["metadata"]["annotations"]
        self.templates[name].update(annotations)
        return {}


def _secret(data: dict[str, str] | None = None, **annotations: str) -> dict:
    return {
        "metadata": {"annotations": dict(annotations)},
        "data": {
            k: base64.b64encode(v.encode()).decode() for k, v in (data or {}).items()
        },
    }


@pytest.fixture
def script(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("token")
    (tmp_path / "ca.crt").write_text("")
    rows = tmp_path / "rows"
    for key, value in {
        "SA_DIR": str(tmp_path),
        "KUBERNETES_SERVICE_HOST": "kubernetes",
        "KUBERNETES_SERVICE_PORT": "443",
        "NAMESPACE": "factory",
        "ITSAPLAN_SECRET": "factory-itsaplan",
        "ADMIN_SECRET": "factory-itsaplan-admin",
        "ADMIN_EMAIL": "admin@example.invalid",
        "DB_ROWS": str(rows),
        "PLAN": str(tmp_path / "plan"),
        "ROLL": " ".join(_DEPLOYMENTS),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("ssl.create_default_context", lambda **_: None)
    spec = importlib.util.spec_from_file_location("secret_init", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    def run(api: FakeAPI, db_rows: int) -> None:
        rows.write_text(f"{db_rows}\n")
        monkeypatch.setattr(module, "call", api.call)
        module.apply()

    return run


def _admin() -> dict:
    return _secret({"email": "admin@example.invalid", "password": "p"})


def test_regeneration_stores_the_generation_with_the_keys(script) -> None:
    api = FakeAPI(
        {
            "factory-itsaplan": _secret(**{_ALLOW: "true"}),
            "factory-itsaplan-admin": _admin(),
        }
    )
    script(api, 7)
    main = api.secrets["factory-itsaplan"]
    assert set(main["data"]) == {"BETTER_AUTH_SECRET", "APP_ENCRYPTION_KEY"}
    generation = main["metadata"]["annotations"][_ROLL]
    assert _ALLOW not in main["metadata"]["annotations"]
    assert api.puts == ["factory-itsaplan"]
    assert all(t[_ROLL] == generation for t in api.templates.values())


def test_a_failed_restart_is_completed_by_the_next_run(script) -> None:
    api = FakeAPI(
        {
            "factory-itsaplan": _secret(**{_ALLOW: "true"}),
            "factory-itsaplan-admin": _admin(),
        }
    )
    api.fail_patch = True
    with pytest.raises(SystemExit):
        script(api, 7)
    generation = api.secrets["factory-itsaplan"]["metadata"]["annotations"][_ROLL]
    assert all(_ROLL not in t for t in api.templates.values())

    api.fail_patch = False
    script(api, 7)
    assert api.puts == ["factory-itsaplan"]  # the keys are not written again
    assert all(t[_ROLL] == generation for t in api.templates.values())


def test_keys_without_a_generation_restart_nothing(script) -> None:
    keys = {"BETTER_AUTH_SECRET": "a", "APP_ENCRYPTION_KEY": "b"}
    api = FakeAPI(
        {"factory-itsaplan": _secret(keys), "factory-itsaplan-admin": _admin()}
    )
    script(api, 7)
    assert api.puts == []
    assert all(not t for t in api.templates.values())
