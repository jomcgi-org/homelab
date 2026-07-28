# BuildBuddy usage snapshots

Each measurement is stored as one JSON file. These files are committed on purpose because BuildBuddy free-tier invocation retention is finite, and an uncommitted baseline is unrecoverable once it ages out.

The earliest snapshot file is the baseline used to measure the 50% reduction target. Regenerate snapshots with:

```sh
python3 bazel/tools/buildbuddy/bb_usage.py snapshot
```
