# oominf weight format (v0)

A converted model is a directory. It is written once by `oominf convert` from an
upstream release checkpoint and read by the engine. Weights keep the precision
and quantisation the release ships in; conversion only re-lays bytes out.

```
<model>.oom/
  index.json      # format tag, version, tensors, expert groups, checksums
  dense.bin       # every non-expert, non-table tensor
  experts.bin     # routed experts: one record per (layer, expert)
  tables.bin      # large row-gathered tables (e.g. Qwen PLE n-gram shards)
  config.json, tokenizer.json, chat_template.jinja, ...   # copied verbatim
```

## Design goals

1. **One record per expert.** A miss is one contiguous read and one
   host-to-device copy, and the tiers keep one slot, checksum and residency
   entry per expert. On NVMe this is about as fast as reading the same bytes
   as six separate parts (drives split large reads into 128 KiB commands
   either way); the win is in copies and bookkeeping, not disk throughput.
2. **Sequential layer streaming.** A layer's records are contiguous, so
   streaming a whole layer (prefill) is one sequential range.
3. **Direct I/O everywhere.** Every tensor and record starts on a 4096-byte
   boundary and is padded to a multiple of 4096, so any of them can be read
   with O_DIRECT into an aligned buffer.
4. **No silent misreads.** Quantised data carries a layout tag; a loader that
   does not implement the tag refuses the file. Every tensor and record has an
   xxh3-64 checksum.
5. **Model-agnostic reader.** The reader knows tensors, records and tables, not
   models. Mapping upstream names to records is the converter's model adapter.

## Constants

- Byte order: little-endian.
- `ALIGN = 4096`: file-level alignment for tensors, records and tables.
- `PART_ALIGN = 256`: alignment of each part inside an expert record (enough
  for vectorised GPU loads from a record copied verbatim into a VRAM slot).

## dense.bin and tables.bin

A sequence of tensors, each at an `ALIGN`-aligned offset and zero-padded to a
multiple of `ALIGN`. Tensor bytes are the upstream bytes unchanged (row-major).
Tables differ from dense tensors only in placement: they are large, read by row
gather rather than loaded whole, and live in their own file so they can sit on
a different tier.

## experts.bin

Grouped by layer, layer-major then expert-minor. Every record in a group shares
one **record schema**: an ordered list of parts, each with name, dtype, shape,
offset within the record (a multiple of `PART_ALIGN`) and byte length. The
record stride is the schema size rounded up to `ALIGN`.

```
group(layer L) at offset G_L:
  record(L, e) at G_L + e * stride        for e in 0..num_experts
```

Padding bytes are zero.

### Layout tags

Each schema has a `layout` tag naming how its parts are encoded. v0 defines
two:

- `nvfp4-modelopt-g16`: NVIDIA ModelOpt NVFP4 W4A4 as released. Per projection
  `p` in `gate`, `up`, `down`: `p.weight` U8 (two E2M1 values per byte, row-major,
  low nibble first), `p.weight_scale` F8_E4M3 (one per 16 inputs, row-major,
  linear, not swizzled), `p.weight_scale_2` F32 scalar, `p.input_scale` F32 scalar.
- `bf16`: unquantised weights as released. Parts `gate.weight` and `up.weight`
  BF16 `[inter, hidden]` and `down.weight` BF16 `[hidden, inter]`, row-major.

Kernel-specific repacks (swizzled scales, fused gate/up, interleaving) get
their own tags and their own converted copy; v0 stores only the release layout.

## index.json

```jsonc
{
  "format": "oominf",
  "version": 0,
  "source": { "origin": "org/repo@revision", "model_type": "qwen4_exp", "fingerprint": "..." },
  "files": { "dense": { "bytes": N }, "experts": { "bytes": N }, "tables": { "bytes": N } },
  "tensors": [
    { "name": "...", "file": "dense" | "tables", "dtype": "BF16", "shape": [..],
      "offset": N, "nbytes": N, "xxh3": "hex16" }
  ],
  "expert_groups": [
    { "layer": 0, "num_experts": 512, "offset": N, "stride": N,
      "layout": "nvfp4-modelopt-g16",
      "parts": [ { "name": "gate.weight", "dtype": "U8", "shape": [640, 1280],
                   "offset": N, "nbytes": N } ],
      "xxh3": ["hex16", ...] }
  ]
}
```

- Record checksums cover the whole `stride` bytes, padding included, so a raw
  record read can be verified without parsing it.
- dtypes use safetensors spellings (`BF16`, `F32`, `U8`, `F8_E4M3`, `I64`, ...).

## Qwen 3.8 Flash adapter (v0)

- Routed experts `model.language_model.layers.{L}.mlp.experts.{E}.{gate,up,down}_proj.*`
  become records. Record part order: the six F32 scalars first (one 256-byte
  part, `scalars`, ordered gate.weight_scale_2, gate.input_scale,
  up.weight_scale_2, up.input_scale, down.weight_scale_2, down.input_scale),
  then gate.weight, gate.weight_scale, up.weight, up.weight_scale,
  down.weight, down.weight_scale.
- `*.ple.ple_embedding.ngram_embedding.shard_*.weight` go to `tables.bin`.
- The multi-token-prediction (MTP) layer's routed experts, released stacked as
  `mtp.layers.0.mlp.experts.gate_up_proj` `[experts, 2 * inter, hidden]` (gate
  rows, then up rows) and `mtp.layers.0.mlp.experts.down_proj`
  `[experts, hidden, inter]`, become expert group 48 (after the decoder layers)
  with layout `bf16`: each record is one expert's slices.
- `model.visual.*` is skipped (text-only).
- Everything else, the rest of the MTP layer included, goes to `dense.bin`.

Record size: 256 + 3 x 819,200 + 3 x 102,400 = 2,765,056 bytes, stride
2,768,896 (676 x 4096). 512 experts x 48 layers is about 68 GB. MTP records are
3 x 3,276,800 = 9,830,400 bytes (2,400 x 4096); 512 of them are about 4.7 GiB.
