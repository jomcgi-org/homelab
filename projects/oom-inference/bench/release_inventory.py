#!/usr/bin/env python3
"""Sum the text-model tensor bytes of the pinned upstream release.

Reads only safetensors headers (HTTP range requests, or a local checkpoint
directory), then groups tensors with the converter's rules in
crates/oominf-convert/src/adapters/qwen38.rs: vision tensors are excluded,
routed experts and the fused MTP experts are counted separately, PLE n-gram
shards are the gather tables, and everything else is dense. GB is 10^9 bytes.

    python3 bench/release_inventory.py            # pinned revision, over HTTP
    python3 bench/release_inventory.py /path/to/checkpoint
"""

import json
import re
import struct
import sys
import urllib.request
from collections import Counter
from pathlib import Path

REPO = "RadixArk/Qwen3.8-Flash-Next-NVFP4"
REVISION = "7b719225242aacd3dbd3f9407468c2ee9a9d2594"
BASE = f"https://huggingface.co/{REPO}/resolve/{REVISION}/"
EXPERT = re.compile(r"^model\.language_model\.layers\.\d+\.mlp\.experts\.\d+\.")
MTP_EXPERTS = {
    "mtp.layers.0.mlp.experts.gate_up_proj",
    "mtp.layers.0.mlp.experts.down_proj",
}


def fetch(name, start=0, length=None):
    if isinstance(BASE_DIR, Path):
        with open(BASE_DIR / name, "rb") as f:
            f.seek(start)
            return f.read(length) if length is not None else f.read()
    headers = {}
    if length is not None:
        headers["Range"] = f"bytes={start}-{start + length - 1}"
    req = urllib.request.Request(BASE + name, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def header(shard):
    (n,) = struct.unpack("<Q", fetch(shard, 0, 8))
    return json.loads(fetch(shard, 8, n))


def size(meta):
    start, end = meta["data_offsets"]
    return end - start


def classify(name):
    if name.startswith("model.visual."):
        return "vision (excluded)"
    if EXPERT.match(name):
        return "decoder experts"
    if name in MTP_EXPERTS:
        return "MTP experts"
    if ".ple.ple_embedding.ngram_embedding.shard_" in name and name.endswith(".weight"):
        return "PLE tables"
    return "dense"


BASE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if BASE_DIR is None:
    BASE_DIR = BASE
shards = sorted(set(json.loads(fetch("model.safetensors.index.json"))["weight_map"].values()))
totals = Counter()
for shard in shards:
    print(shard, file=sys.stderr, flush=True)
    for name, meta in header(shard).items():
        if name != "__metadata__":
            totals[classify(name)] += size(meta)
text = sum(v for k, v in totals.items() if not k.startswith("vision"))
for k, v in sorted(totals.items()):
    print(f"{k:20} {v / 1e9:8.2f} GB")
print(f"{'text-model total':20} {text / 1e9:8.2f} GB  ({text / 2**30:.2f} GiB)")
