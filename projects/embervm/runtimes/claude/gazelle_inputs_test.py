"""The packaged generator's provenance rejects stale source, not app edits."""

import json

import pytest

import gazelle_inputs


def test_manifest_rejects_changed_or_missing_extension(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "extension.go"
    source.write_text("package extension\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"extension.go": gazelle_inputs.digest("extension.go")})
    )
    gazelle_inputs.verify(manifest)
    source.write_text("package changed\n")
    with pytest.raises(RuntimeError, match="extension.go.*rebuild"):
        gazelle_inputs.verify(manifest)
    source.unlink()
    with pytest.raises(RuntimeError, match="extension.go.*rebuild"):
        gazelle_inputs.verify(manifest)


def test_root_build_hash_tracks_only_generator_declaration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "BUILD"
    source.write_text('gazelle_binary(\n    name = "gazelle_binary",\n)\n')
    original = gazelle_inputs.digest("BUILD")
    assert gazelle_inputs.digest("./BUILD") == original
    source.write_text(source.read_text() + '\nfilegroup(name = "new_app")\n')
    assert gazelle_inputs.digest("BUILD") == original
    source.write_text(source.read_text().replace('"gazelle_binary"', '"different"'))
    assert gazelle_inputs.digest("BUILD") != original
    source.write_text("# no generator\n")
    with pytest.raises(ValueError, match="declaration"):
        gazelle_inputs.digest("BUILD")


@pytest.mark.parametrize("filename", ["go.mod", "go.sum"])
def test_go_dependency_hash_tracks_extension_dependency(
    tmp_path, monkeypatch, filename
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / filename
    source.write_text("sigs.k8s.io/yaml v1.0.0\n")
    original = gazelle_inputs.digest(filename)
    assert gazelle_inputs.digest("./" + filename) == original
    source.write_text(source.read_text() + "application.example/lib v2.0.0\n")
    assert gazelle_inputs.digest(filename) == original
    source.write_text(source.read_text().replace("v1.0.0", "v1.1.0"))
    assert gazelle_inputs.digest(filename) != original


def test_module_hash_ignores_unrelated_pins_but_tracks_binary_versions(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "MODULE.bazel"
    generator = 'bazel_dep(name = "gazelle", version = "0.47.0")\n'
    source.write_text(generator + 'bazel_dep(name = "rules_apko", version = "1.0")\n')
    original = gazelle_inputs.digest("MODULE.bazel")
    source.write_text(
        generator
        + 'bazel_dep(name = "rules_apko", version = "2.0")\nhttp_archive(name = "new_chart", sha256 = "new")\n'
    )
    assert gazelle_inputs.digest("MODULE.bazel") == original
    source.write_text(source.read_text().replace('"0.47.0"', '"0.48.0"'))
    assert gazelle_inputs.digest("MODULE.bazel") != original


@pytest.mark.parametrize("module", sorted(gazelle_inputs.BINARY_MODULES))
def test_module_hash_tracks_each_plugin_and_overrides(tmp_path, monkeypatch, module):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "MODULE.bazel"
    source.write_text(f'bazel_dep(name = "{module}", version = "1.0")\n')
    original = gazelle_inputs.digest("MODULE.bazel")
    source.write_text(
        source.read_text()
        + f'single_version_override(module_name = "{module}", version = "2.0")\n'
    )
    assert gazelle_inputs.digest("MODULE.bazel") != original


@pytest.mark.parametrize("module", sorted(gazelle_inputs.EXTENSION_GO_MODULES))
def test_extension_transitive_yaml_versions_are_fingerprinted(
    tmp_path, monkeypatch, module
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "go.mod"
    source.write_text(f"sigs.k8s.io/yaml v1.6.0\n{module} v2.0.0\n")
    original = gazelle_inputs.digest("go.mod")
    source.write_text(source.read_text().replace("v2.0.0", "v2.0.1"))
    assert gazelle_inputs.digest("go.mod") != original
