import io
import json
from pathlib import Path

import pytest

from measure_chunks import (
    ChunkingParameters,
    ImageSpec,
    iter_chunk_records,
    load_specs,
    measure,
)


def test_chunking_is_deterministic_across_read_sizes():
    payload = (b"wolfi-base\0" * 800) + (b"cli-layer\0" * 600)
    params = ChunkingParameters(minimum=64, average=128, maximum=512)

    small_reads = list(iter_chunk_records(io.BytesIO(payload), params, read_size=37))
    large_reads = list(iter_chunk_records(io.BytesIO(payload), params, read_size=4096))

    assert small_reads == large_reads
    assert sum(chunk.size for chunk in small_reads) == len(payload)
    assert all(0 < chunk.size <= params.maximum for chunk in small_reads)


def test_fixed_baseline_uses_average_as_the_chunk_size():
    payload = bytes(range(256)) * 5
    params = ChunkingParameters(
        minimum=64,
        average=256,
        maximum=512,
        algorithm="fixed-v1",
    )

    chunks = list(iter_chunk_records(io.BytesIO(payload), params, read_size=71))

    assert [chunk.size for chunk in chunks] == [256, 256, 256, 256, 256]


def test_scope_report_separates_account_and_principal_reuse(tmp_path: Path):
    payload = b"shared-rootfs-content\0" * 200
    paths = []
    for name in ("one", "two", "three"):
        path = tmp_path / f"{name}.erofs"
        path.write_bytes(payload)
        paths.append(path)

    specs = [
        ImageSpec("one", "account-a", "principal-a", paths[0]),
        ImageSpec("two", "account-a", "principal-b", paths[1]),
        ImageSpec("three", "account-b", "principal-a", paths[2]),
    ]
    report = measure(
        specs,
        ChunkingParameters(minimum=64, average=128, maximum=512),
        read_size=97,
    )

    logical = len(payload) * 3
    one_image_stored = report["images"][0]["new_bytes"]["account"]
    assert report["logical_bytes"] == logical
    assert report["scopes"]["per_image"]["stored_bytes"] == len(payload) * 3
    assert report["scopes"]["principal"]["stored_bytes"] == one_image_stored * 3
    assert report["scopes"]["account"]["stored_bytes"] == one_image_stored * 2
    assert report["scopes"]["global"]["stored_bytes"] == one_image_stored
    assert one_image_stored > 0
    assert report["images"][1]["new_bytes"]["account"] == 0
    assert report["images"][2]["new_bytes"]["account"] == one_image_stored


def test_load_specs_resolves_relative_paths_and_rejects_duplicate_names(tmp_path: Path):
    image = tmp_path / "image.erofs"
    image.write_bytes(b"rootfs")
    manifest = tmp_path / "images.json"
    manifest.write_text(
        json.dumps(
            {
                "format_version": 1,
                "images": [
                    {
                        "name": "runtime",
                        "account": "homelab",
                        "principal": "platform",
                        "path": image.name,
                    }
                ],
            }
        )
    )

    specs = load_specs(manifest)
    assert specs == [ImageSpec("runtime", "homelab", "platform", image)]

    raw = json.loads(manifest.read_text())
    raw["images"].append(dict(raw["images"][0]))
    manifest.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="image names must be unique"):
        load_specs(manifest)


@pytest.mark.parametrize(
    "params, message",
    [
        (ChunkingParameters(0, 128, 512), "minimum chunk size"),
        (ChunkingParameters(256, 128, 512), "minimum <= average <= maximum"),
        (ChunkingParameters(64, 192, 512), "power of two"),
        (ChunkingParameters(64, 128, 512, "unknown"), "chunk algorithm"),
    ],
)
def test_invalid_chunk_parameters_fail_closed(params, message):
    with pytest.raises(ValueError, match=message):
        list(iter_chunk_records(io.BytesIO(b"content"), params))
