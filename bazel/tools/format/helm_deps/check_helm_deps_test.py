"""Pin the pure logic of check_helm_deps.py.

The checker itself runs as plain python3 against the full working tree from the
CI format step (a sandboxed bazel test cannot list projects/* on RBE), so what
is pinned here is the decision logic, not the walk.
"""

from __future__ import annotations

import io
import tarfile

from check_helm_deps import (
    bare_tag_refs,
    declared_version,
    diff_entries,
    parse_managed_paths,
    source_entries,
    tarball_entries,
)


def _tgz(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in members.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


# --- tarball freshness -----------------------------------------------------


def test_identical_templates_are_not_stale():
    packaged, _ = tarball_entries(
        _tgz({"lib/templates/_a.tpl": "x", "lib/Chart.yaml": "version: 1.0.0"})
    )
    assert diff_entries(packaged, {"lib/templates/_a.tpl": b"x"}) == []


def test_changed_template_is_stale():
    packaged, _ = tarball_entries(_tgz({"lib/templates/_a.tpl": "old"}))
    assert diff_entries(packaged, {"lib/templates/_a.tpl": b"new"}) == [
        "lib/templates/_a.tpl"
    ]


def test_added_and_removed_templates_are_both_stale():
    packaged, _ = tarball_entries(_tgz({"lib/templates/_a.tpl": "x"}))
    assert diff_entries(packaged, {"lib/templates/_b.tpl": b"x"}) == [
        "lib/templates/_a.tpl",
        "lib/templates/_b.tpl",
    ]


def test_chart_yaml_reserialisation_is_not_drift():
    """`helm package` alphabetises keys and unquotes strings.

    This is the false positive that would have made the check useless: every
    chart would read as permanently stale. Chart.yaml is compared on version
    alone, so it must not appear in the byte-compared entry set.
    """
    packaged, version = tarball_entries(
        _tgz({"lib/Chart.yaml": "annotations:\n  a: b\nname: lib\nversion: 0.6.0\n"})
    )
    assert packaged == {}
    assert version == "0.6.0"


def test_version_is_read_regardless_of_key_order_and_quoting():
    assert declared_version('name: lib\nversion: "0.6.0"\n') == "0.6.0"
    assert declared_version("version: 0.6.0\nname: lib\n") == "0.6.0"
    assert declared_version("name: lib\n") is None


def test_chart_lock_is_ignored():
    """Chart.lock carries a resolution timestamp, so it always differs."""
    packaged, _ = tarball_entries(
        _tgz({"lib/Chart.lock": "generated: 2026-01-01", "lib/templates/_a.tpl": "x"})
    )
    assert set(packaged) == {"lib/templates/_a.tpl"}


def test_source_entries_key_on_chart_name_not_directory(tmp_path):
    """homelab-library lives in `.../homelab-library/chart`, cf-ingress in
    `.../cf-ingress-library`. Keying on the directory makes every member look
    both added and removed, i.e. every chart permanently stale."""
    chart_dir = tmp_path / "chart"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text("name: homelab-library\nversion: 0.6.0\n")
    (chart_dir / "templates" / "_a.tpl").write_text("x")
    assert set(source_entries(chart_dir)) == {"homelab-library/templates/_a.tpl"}


# --- digest-not-tag --------------------------------------------------------

_MANAGED = {"web.image", "controllerManager.image"}


def test_bare_tag_on_a_managed_image_is_flagged():
    line = 'image: "{{ .Values.web.image.repository }}:{{ .Values.web.image.tag }}"'
    assert bare_tag_refs(line, _MANAGED)


def test_digest_render_is_accepted():
    line = 'image: "{{ .Values.web.image.repository }}@{{ .Values.web.image.digest }}"'
    assert bare_tag_refs(line, _MANAGED) == []


def test_tag_plus_digest_is_accepted():
    """embervm tokenBroker renders `repo:tag@digest`; the digest still decides."""
    line = 'image: "{{ .Values.web.image.repository }}:{{ .Values.web.image.tag }}@{{ .Values.web.image.digest }}"'
    assert bare_tag_refs(line, _MANAGED) == []


def test_unmanaged_upstream_image_is_ignored():
    """Nothing injects a digest for an upstream image and nothing skips pushing
    it, so a bare tag there is correct (cloudflare-gateway envoy, embervm
    servingEnvoy)."""
    line = 'image: "{{ .Values.servingEnvoy.image.repository }}:{{ .Values.servingEnvoy.image.tag }}"'
    assert bare_tag_refs(line, _MANAGED) == []


def test_tag_behind_a_helper_include_is_still_flagged():
    """dashboard-sidecar hid its tag behind `include "....imageTag"`, which is
    why matching on the whole rendered string matters, not just `.tag`."""
    line = 'image: "{{ .Values.controllerManager.image.repository }}:{{ include "x.imageTag" . }}"'
    assert bare_tag_refs(line, _MANAGED)


def test_non_image_lines_are_ignored():
    assert (
        bare_tag_refs('  repository: "{{ .Values.web.image.repository }}"', _MANAGED)
        == []
    )


# --- managed path extraction ----------------------------------------------


def test_managed_paths_parsed_from_helm_chart():
    build = """
helm_chart(
    name = "chart",
    images = {
        "web.image": "//projects/monolith:image_public.info",
        "frontend.image": "//projects/monolith/frontend:image_public.info",
    },
    publish = True,
)
"""
    assert parse_managed_paths(build) == {"web.image", "frontend.image"}


def test_chart_without_images_map_manages_nothing():
    assert parse_managed_paths('helm_chart(\n    name = "chart",\n)\n') == set()
