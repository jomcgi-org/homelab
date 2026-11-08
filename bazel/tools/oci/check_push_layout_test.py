import hashlib
import json

import pytest

from check_push_layout import check_layout, main


def image(tmp_path):
    layout = tmp_path / "image"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def blob(content):
        digest = hashlib.sha256(content).hexdigest()
        path = blobs / digest
        path.write_bytes(content)
        return {"digest": f"sha256:{digest}", "size": len(content)}, path

    config, _ = blob(b"{}")
    layer, layer_path = blob(b"layer bytes")
    manifest, _ = blob(json.dumps({"config": config, "layers": [layer]}).encode())
    index, _ = blob(json.dumps({"manifests": [manifest]}).encode())
    (layout / "index.json").write_text(json.dumps({"manifests": [index]}))
    return layout, layer_path


def test_nested_layout_with_external_layer_link_is_readable(tmp_path):
    layout, layer = image(tmp_path)
    target = tmp_path / "layer.tar"
    layer.rename(target)
    layer.symlink_to(target)
    check_layout(layout)


@pytest.mark.parametrize("dangling_link", [False, True])
def test_missing_layer_reports_exact_blob_and_link(tmp_path, dangling_link):
    layout, layer = image(tmp_path)
    layer.unlink()
    if dangling_link:
        layer.symlink_to(tmp_path / "missing.tar")
    with pytest.raises(
        ValueError, match=f"Unreadable OCI blob sha256:{layer.name}"
    ) as error:
        check_layout(layout)
    assert "resolved=" in str(error.value)
    assert ("missing.tar" in str(error.value)) == dangling_link


def test_truncated_layer_is_rejected(tmp_path):
    layout, layer = image(tmp_path)
    layer.write_bytes(b"short")
    with pytest.raises(ValueError, match="size 5 != 11"):
        check_layout(layout)


def test_wrapper_never_executes_appended_push_command(tmp_path):
    layout, _ = image(tmp_path)
    marker = tmp_path / "pushed"
    assert main([str(layout), "touch", str(marker)]) == 0
    assert not marker.exists()


def test_missing_layout_refuses_without_running_push(tmp_path):
    marker = tmp_path / "pushed"
    assert main([str(tmp_path / "absent"), "touch", str(marker)]) == 1
    assert not marker.exists()
