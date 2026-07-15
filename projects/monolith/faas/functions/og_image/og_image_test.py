"""Tests for the og-image function (handler render + deterministic bundling).

Two concerns are covered here:

1. The handler contract (``app.handle`` / ``app.render_png``): a real Pillow
   render, so the PNG path is verified in CI, not only live. Pillow is a monolith
   pip dep (``@pip//pillow``), so ``import PIL`` works in the test sandbox.
2. The registration bundler (``register.build_archive``): the sha256 must be
   stable across rebuilds, because "idempotent by zip sha" (Task 12) depends on
   the same ``app.py`` always producing the same archive bytes.
"""

from __future__ import annotations

import base64
import io

from PIL import Image

from faas.functions.og_image import app, register

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _decode_body(response: dict) -> bytes:
    assert response["isBase64Encoded"] is True
    return base64.b64decode(response["body"])


def test_render_png_is_valid_and_correctly_sized():
    png = app.render_png("Hello EmberVM", "the zip lane, live")
    assert png.startswith(_PNG_MAGIC)
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    assert image.size == (app.WIDTH, app.HEIGHT)


def test_render_is_deterministic_for_same_inputs():
    # Restore-safe contract: no wall-clock, no entropy, so identical inputs must
    # render identical bytes (every restored invoke is reproducible).
    first = app.render_png("Same", "inputs")
    second = app.render_png("Same", "inputs")
    assert first == second


def test_handle_returns_png_response_shape():
    event = {"queryStringParameters": {"title": "Deploys", "subtitle": "shipped"}}
    response = app.handle(event, None)

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "image/png"
    png = _decode_body(response)
    assert png.startswith(_PNG_MAGIC)
    assert Image.open(io.BytesIO(png)).size == (app.WIDTH, app.HEIGHT)


def test_handle_defaults_when_no_query_params():
    # No queryStringParameters at all (the shim passes None for an empty query):
    # falls back to the default title and an empty subtitle, still a valid PNG.
    for event in ({"queryStringParameters": None}, {}):
        response = app.handle(event, None)
        assert response["statusCode"] == 200
        assert _decode_body(response).startswith(_PNG_MAGIC)


def test_handle_clamps_overlong_title():
    long_title = "x" * (app.TITLE_MAX + 50)
    event = {"queryStringParameters": {"title": long_title}}
    # Render must not raise or hang on a very long, wrap-defying input.
    response = app.handle(event, None)
    assert response["statusCode"] == 200
    assert _decode_body(response).startswith(_PNG_MAGIC)


def test_build_archive_is_deterministic():
    # The idempotency-by-sha invariant: the same source bytes always yield the
    # same archive bytes (fixed member name/timestamp/mode/compression).
    source = b"def handle(event, context):\n    return {'statusCode': 200}\n"
    first = register.build_archive(source)
    second = register.build_archive(source)
    assert first == second
    assert register.archive_sha256(source) == register.archive_sha256(source)


def test_build_archive_contains_app_py_at_root():
    import zipfile

    archive = register.build_archive(b"# marker\n")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.namelist() == ["app.py"]
        assert zf.read("app.py") == b"# marker\n"


def test_default_archive_imports_the_real_handler():
    # The checked-in app.py packs cleanly and exposes app.handle at the root, so
    # the shim's default EMBER_HANDLER=app.handle resolves inside the guest.
    import zipfile

    archive = register.build_archive()
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        source = zf.read("app.py").decode("utf-8")
    assert "def handle(" in source
