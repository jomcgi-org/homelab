# EmberVM rootfs sizing tools

`measure_chunks.py` is the Phase 0 sizing harness for ADR embervm/028. It
measures content-defined chunk reuse across flattened filesystem images without
pulling OCI images, creating block devices, or writing to the artifact store.
It is a measurement tool, not the production chunker, and its format does not
freeze the eventual on-disk manifest.

The input is a JSON document. Paths may be absolute or relative to the input
document.

```json
{
  "format_version": 1,
  "images": [
    {
      "name": "runtime-python",
      "account": "homelab",
      "principal": "platform",
      "path": "out/runtime-python.erofs"
    },
    {
      "name": "runtime-claude",
      "account": "homelab",
      "principal": "agents",
      "path": "out/runtime-claude.erofs"
    }
  ]
}
```

Run it directly after producing deterministic flattened images:

```shell
python3 projects/embervm/rootfs/measure_chunks.py images.json > report.json
```

For the fixed-offset baseline required by the one-package-rebuild experiment,
run the same input with `--algorithm fixed-v1`. The `--average` value is the
fixed chunk size in that mode. Compare it with the default `gear-v1` report.

The report compares four physical-storage models over the same chunk stream:

- `per_image`: no sharing between images, equivalent to materializing every
  flattened image separately.
- `principal`: chunks deduplicate only within one principal.
- `account`: chunks deduplicate across all principals in one account.
- `global`: every identical chunk deduplicates, an upper bound rather than the
  decided private-image policy.

Ratios exclude manifests, encryption metadata, and filesystem allocation
overhead. Input order affects each image's `new_bytes` attribution but never the
aggregate scope totals.
