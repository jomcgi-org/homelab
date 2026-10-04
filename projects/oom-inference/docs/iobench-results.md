# Weight-layout I/O microbench results (2026-10-04)

Question: do expert-major records (`docs/FORMAT.md`) load routed experts from
NVMe faster than a tensor-major layout, enough to justify the format?

**Verdict: on raw disk I/O, no material difference.** Expert-major is 0 to 3%
faster in interleaved A/B (within noise; one run showed 0.95 at k=1). Both
layouts are bandwidth-bound: the device splits every read into 128 KiB
commands (`max_hw_sectors_kb = 128`), so one 2.7 MB read and six 0.1 to 0.8 MB
reads become about the same command stream. The format is still worth keeping
for reasons this bench does not measure (see "What the bench does not cover"),
but not for disk throughput.

**The big lever is the hit rate, not the layout.** On the recorded decode
trace, an all-miss step costs about 200 ms (10 experts x 48 layers at about
6.5 GB/s); a per-layer LRU of 128 experts (25% of experts) cuts misses from 10
to 0.63 per layer and the step to about 17 ms.

## Setup

- Device: KINGSTON SKC3000D2048G (KC3000 2 TB, PCIe 4.0), firmware EIFK31.6,
  logical block 512 B, `max_hw_sectors_kb` 128, `nr_requests` 1023. Kernel
  6.8.0-142. Filesystem on `/disks/nvme-02`.
- Crate `crates/oominf-iobench`, io_uring (`io-uring` 0.7) with O_DIRECT into
  4096-aligned, pre-faulted buffers, one 2,768,896-byte slot per expert.
- Data: synthetic random bytes with exact Qwen 3.8 Flash record geometry, 3
  layers x 512 experts per layout (4.25 GB each).
  - `expert`: one 4096-aligned record per expert, **1 read** per expert.
  - `tensor`: each part type as one contiguous `[512, ..]` array per layer,
    **6 reads** per expert (the six weight and scale parts; the F32 scalars
    are assumed resident, which favours this layout).
  - Same bytes delivered per expert (2.76 MB).
- Traces:
  - `trace-routedump1`: 3840 recorded decode steps x 48 layers x top-10 from the
    old fork's router dump (`results/prefill-depth-20260922/routedump1`), saved
    as `/disks/nvme-02/src/oominf-data/iobench/trace-routedump1.u16`. Mean
    step-to-step overlap: 2.5 of 10 experts per layer.
  - `synthetic-zipf1.0`: Zipf(s=1.0) per layer with 2 to 3 experts carried over
    per step.
  - Trace layer `l` maps to disk layer `l % 3`.
- Cache model: per-layer LRU of 0 (all miss), 64 or 128 experts, warmed for
  256 untimed steps.
- Engines: io_uring with 1 or 4 layers in flight; blocking `pread` pool of 10
  threads, one layer at a time.
- Run under `nice -n 19 taskset -c 8-15`, no GPU. The FreeToken production
  service was running (its keepwarm timer issues a request every minute) and
  another workstream was writing to the same disk, which explains the long
  p95/p99 tails in some rows. The interleaved A/B below is the robust
  comparison.

## Interleaved A/B (headline)

Each sample loads k random experts of a random layer with every read submitted
at once, once per layout, alternating which layout goes first. 1000 samples per
k per layout. Two full runs:

| k | run | expert p50 us | tensor p50 us | median paired ratio tensor/expert |
|---|---|---|---|---|
| 1 | 1 | 495 | 505 | 1.020 |
| 1 | 2 | 887 | 847 | 0.953 |
| 2 | 1 | 887 | 896 | 1.009 |
| 2 | 2 | 920 | 925 | 0.979 |
| 4 | 1 | 1688 | 1753 | 1.035 |
| 4 | 2 | 1681 | 1730 | 1.026 |
| 10 | 1 | 4026 | 4133 | 1.029 |
| 10 | 2 | 4052 | 4099 | 1.000 |

Run 2 had more background I/O (k=1 p50 nearly doubled for both layouts). A
single expert takes about 0.5 ms when the disk is quiet (5.6 GB/s); 10 experts
take about 4 ms (6.8 GB/s).

## Decode-miss replay (48 timed steps x 48 layers, median of 3 runs)

Recorded trace:

| layout | LRU/layer | engine | miss/layer | reads/layer | p50 us | p99 us | ms/step | GB/s |
|---|---|---|---|---|---|---|---|---|
| expert | none | uring x1 | 10.00 | 10.0 | 4081 | 7949 | 213.9 | 6.21 |
| tensor | none | uring x1 | 10.00 | 60.0 | 4086 | 4898 | 200.7 | 6.61 |
| expert | none | uring x4 | 10.00 | 10.0 | 15639 | 26102 | 189.2 | 7.03 |
| tensor | none | uring x4 | 10.00 | 60.0 | 16029 | 26248 | 200.2 | 6.63 |
| expert | none | pread t10 | 10.00 | 10.0 | 4656 | 10993 | 251.8 | 5.28 |
| tensor | none | pread t10 | 10.00 | 60.0 | 4759 | 11416 | 260.1 | 5.10 |
| expert | 64 | uring x1 | 3.26 | 3.3 | 1652 | 5018 | 76.7 | 5.65 |
| tensor | 64 | uring x1 | 3.26 | 19.6 | 1644 | 5335 | 80.3 | 5.39 |
| expert | 64 | uring x4 | 3.26 | 3.3 | 5391 | 12991 | 59.6 | 7.27 |
| tensor | 64 | uring x4 | 3.26 | 19.6 | 5519 | 13149 | 60.8 | 7.12 |
| expert | 64 | pread t10 | 3.26 | 3.3 | 1652 | 6807 | 85.4 | 5.07 |
| tensor | 64 | pread t10 | 3.26 | 19.6 | 1665 | 7524 | 92.1 | 4.70 |
| expert | 128 | uring x1 | 0.63 | 0.6 | 494 | 3931 | 17.3 | 4.84 |
| tensor | 128 | uring x1 | 0.63 | 3.8 | 589 | 3988 | 18.9 | 4.42 |
| expert | 128 | uring x4 | 0.63 | 0.6 | 2587 | 8006 | 13.4 | 6.24 |
| tensor | 128 | uring x4 | 0.63 | 3.8 | 2640 | 7077 | 13.5 | 6.19 |
| expert | 128 | pread t10 | 0.63 | 0.6 | 527 | 4192 | 18.0 | 4.63 |
| tensor | 128 | pread t10 | 0.63 | 3.8 | 554 | 5060 | 19.9 | 4.19 |

With 4 layers in flight, per-layer latency includes queueing behind earlier
layers, so compare ms/step, not p50, across engines.

The synthetic Zipf trace gave the same picture (raw table in
`/disks/nvme-02/src/oominf-data/iobench/matrix.md`), with one noisy pair
(expert x4 all-miss at 321 ms/step, p99 250 ms) attributable to background I/O.
Notably, synthetic Zipf(1.0) understates locality: LRU-128 leaves 2.58
misses/layer versus 0.63 on the recorded trace, so cache sizing should use
recorded traces.

## Layer stream (prefill-style, all 3 layers, 4 MiB chunks)

| layout | QD 8 GB/s | QD 16 GB/s |
|---|---|---|
| expert | 7.26 | 7.41 |
| tensor | 2.04 (interference outlier) | 7.44 |

Both reach the device's sequential ceiling of about 7.4 GB/s.

## Findings for the engine design

1. **Layout does not matter for NVMe bandwidth** at these part sizes (all
   multiples of 100 KiB). Expert-major's edge is at most a few percent.
2. **Hit rate dominates.** All-miss decode is disk-bound at about 200 ms/step;
   the placement policy and cache capacity are worth 10x, the layout about 1.03x.
3. **Pipelining layers helps throughput:** 4 layers in flight reaches 7.0 to
   7.4 GB/s versus 5.6 to 6.6 GB/s one layer at a time (about 10 to 25% faster
   per step when misses are frequent). This argues for issuing the next
   layers' loads early, which needs routing known ahead of time (prefetch
   prediction), not a different layout.
4. **io_uring beats a 10-thread pread pool** by 5 to 20% per step.
5. A single expert miss costs about 0.5 ms on a quiet disk; a decode step can
   absorb only a handful of serial misses per layer before disk dominates.

## What the bench does not cover

Reasons to keep expert-major records that are outside disk I/O:

- **Host-to-device copies:** one contiguous record is one `cudaMemcpyAsync`
  (or one DMA into a VRAM slot) instead of six. At about 5 to 10 us of launch
  overhead per copy, that could be 25 to 50 us per expert, which is 5 to 10%
  of a 0.5 ms miss. Unmeasured here (no GPU); measure in the GPU spike.
- One slot, one checksum and one residency entry per expert simplifies the
  tiering protocol (and its TLA+ spec).
- Fewer SQEs and completions (1 vs 6 per expert) reduce CPU work on the
  submission path; negligible at these rates.

Recommendation: keep expert-major records in the format (cheap, simpler
tiering, likely helps host-to-device), but do not expect disk-throughput gains
from it; spend optimisation effort on placement policy, cache capacity and
layer-ahead prefetch.

## Reproduce

```text
cargo build --release -p oominf-iobench
B=target/release/oominf-iobench D=/disks/nvme-02/src/oominf-data/iobench
nice -n 19 taskset -c 8-15 $B all --dir $D --layers 3 --trace $D/trace-routedump1.u16 --steps 48 --runs 3 --keep
nice -n 19 taskset -c 8-15 $B ab --dir $D --samples 1000 --k 1,2,4,10
```

`all` deletes the 8.5 GB of layout files unless `--keep` is passed; `ab` needs
them present. The trace file is kept in the data directory.
