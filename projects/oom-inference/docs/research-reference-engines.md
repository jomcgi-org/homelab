# Reference engines and offloaded-MoE research: what applies to oominf (2026-10-04)

Scope: ideas from other engines, papers and our own FreeToken fork that could
speed up oominf on its target regime: 1 to 2 streams, Qwen 3.8 Flash (later GLM
5.3 Flash), RTX 4090 (24 GB, sm_89, PCIe 4.0 x16), 61 GB RAM, Ryzen 7 7800X3D,
KC3000 NVMe (6.5 to 7.4 GB/s). Every idea is judged against the #6825 quality
rules: weights exactly as released, no expert skipping, pruning or
substitution, prediction may only decide what to load, no precision reduction
by default.

Method: external sources were read on 2026-10-04 (several through a
summarising fetcher, so some numbers are paraphrased; anything marked
**[unverified]** was not confirmed from a primary source, **[abs-only]** means
only the abstract was read). FreeToken findings come from the fork's `docs/`,
`bench/RESULTS.md`, `bench/blog-diff-doc.md`, main-line history and about 70
unmerged branches. New numbers in this doc come from replaying the recorded
decode trace (`/disks/nvme-02/src/oominf-data/iobench/trace-routedump1.u16`,
3840 steps x 48 layers x top-10) through cache simulators (section 2.1).

## 0. Budget model this review uses

Numbers that decide which ideas matter, from the converted model
(`qwen38-flash.oom/index.json`) and our measurements:

| Item | Size / cost | Note |
|---|---|---|
| Routed expert record | 2.77 MB (NVFP4 + scales) | 512 x 48 = 24,576 records, 68 GB |
| Routed bytes per token | 10 x 48 x 2.77 MB = 1.33 GB | about 1.5 ms if all read from VRAM |
| Dense weights (bf16, non-PLE, non-MTP) | about 7.3 GB, plus 1.27 GB lm_head | read once per decode step: about 9 to 10 ms at about 0.9 TB/s. This is the **bf16 dense floor** |
| MTP block | 5.2 GB, **its 512 experts are bf16** (about 5.2 MB each) | one draft step touches 10 bf16 experts (about 52 MB) |
| PLE n-gram tables | 51 GB (fp8) | do not fit in RAM next to an expert cache; row gathers from disk |
| VRAM left for expert slots | about 11 to 12 GB (24 GB minus dense, lm_head, KV/state, workspace) | about 4,000 slots, about 85 per layer |
| Host RAM for experts | about 30 to 38 GB realistic (61 GB minus OS, PLE row cache, staging) | about 11k to 14k records, about 230 to 290 per layer |
| Expert miss from disk | about 0.5 ms quiet disk, 10 in parallel about 4 ms | `docs/iobench-results.md` |
| Expert host to VRAM | about 0.11 to 0.13 ms at 22 to 25 GB/s | PCIe 4.0 x16, pinned |
| Expert on CPU from RAM | about 0.05 to 0.09 ms of DDR5 bandwidth (30 to 60 GB/s) | bs=1 is pure bandwidth; FreeToken measured about 30 GB/s achieved vs 60 to 70 ceiling |

CPU: 7800X3D, 8 cores / 16 threads, Zen 4. `lscpu` shows avx512f/bw/vl,
**avx512_vbmi** (vpermb, ideal for a 16-entry E2M1 lookup), avx512_bf16,
avx512_vnni, 96 MB L3. **No AMX.** One NUMA node.

FreeToken production (the baseline we must match; `scripts/serve-qwen-flash-4090.sh`
plus origin/main #99, #117, #121): warm decode about 34.5 tok/s with the hybrid
backend, 34 to 47 tok/s on the ab.py tasks with FP8 dense weights; 100k cold
TTFT about 21.6 s; step about 28 to 35 ms. **It runs FP8 dense weights
(re-quantised from bf16) and an FP8 KV cache.** Both break oominf's rules, so
oominf must find that speed elsewhere (see section 1, "Design consequences").

## 1. Executive summary

### Ranked shortlist

| Rank | Idea | Expected impact (our regime) | Build cost | Quality verdict | Extension point |
|---|---|---|---|---|---|
| 1 | **Frequency-aware tiered expert cache with RAM as a real, persistent tier** (record-granular, not page cache), sized and tuned on recorded traces | Largest single lever. On our trace LFU cuts VRAM misses about 30% vs LRU at 64 to 96 slots/layer; a 230 to 290 slot/layer RAM tier leaves about 0.1 to 0.3 disk misses/layer | Medium | Lossless | expert storage tiers + placement policy |
| 2 | **Hybrid CPU/GPU execution of VRAM misses** (Fiddler, HybriMoE, KTransformers, FreeToken #99): run host-resident misses on AVX-512 with an exact NVFP4 kernel, fetch only a few to the GPU, split per layer by a cost model | Hides most of the 80 to 115 host-tier misses/token behind GPU work; FreeToken's version cut PCIe gather 7.4 to 3.5 ms/token and gave +9% decode even with a 30 GB/s CPU kernel | Medium-high (AVX-512 kernel, scheduler) | Lossless (same weights, fp32 accumulation; only summation order differs) | device backend (CPU expert worker) + placement policy |
| 3 | **One CUDA graph per decode step with a device-side expert slot table and per-layer stream-memop handshakes** | FreeToken's roofline shows the GPU idle about 56% of the step waiting on the CPU; removing launch and sync overhead plus overlapping CPU expert work is worth several ms/token | Medium-high (TLA+ spec change) | Lossless | core decode loop + device backend + tiering protocol |
| 4 | **Exact MTP self-speculative decoding, K=1 to 2, gated at runtime by measured utility** (Cascade) | Amortises the about 9 to 10 ms bf16 dense floor; rough estimate 1.1 to 1.4x warm decode at 60 to 75% acceptance. FreeToken lost 9 to 33% with it, for reasons that mostly do not carry over (section 3.4) | High (GDN rollback, verify kernels, MTP bf16 experts) | Lossless with exact rejection sampling (never ds4's "opportunistic" default). Batch-shape numerics need a parity policy (section 3.4) | core decode loop + model trait |
| 5 | **Layer-major streaming prefill with large token groups** and overlapped disk/host I/O (FreeToken adopted; KTransformers layerwise prefill; DuoServe) | Long-prompt TTFT: FreeToken went 93.7 to 53 s and then to 21.6 s for 100k cold with this plus kernels | Medium | Lossless | prefill strategy |
| 6 | **Disk prefix cache with GDN state snapshots at block boundaries** (FreeToken adopted; SGLang HiCache and Unified Radix Cache; vLLM `mamba_cache_mode`) | Agent workloads: FreeToken 10k-token cold TTFT 870 s to 14.8 s | Medium | Lossless if entries hold exact state; TLA+ invariants already planned | prefix cache backend |
| 7 | **Router-driven lookahead into a separate staging area** (disk to host, never inserted into the cache until used), using the next layer's real router on the current hidden state plus 4-layer-deep io_uring batches | Small once rank 1 exists: disk misses become rare; worth about 10 to 25% only in miss-heavy phases (post-prefill drift, topic changes). FreeToken measured 43 to 48% prediction accuracy and doubled traffic when prefetching into the cache | Low-medium | Lossless (load hint only) | placement policy + storage tiers |
| 8 | **Pinned host arena registered once at startup**, record-sized slots, async H2D on a dedicated copy stream | Enabler for 2, 3 and 7; avoids FreeToken's 0.5 to 1.9 s per 1.32 GiB transient `cudaHostRegister` cost and its torch pinned-allocator leak | Low | Lossless | storage tiers + device backend |

Not recommended (break the rules): KTransformers Expert Deferral (up to 0.5%
accuracy drop), any reduced-top-k demo (KTransformers 6-of-8, Unsloth 4-of-8,
Flash-MoE 4-of-10), HOBBIT, AdapMoE, SiDA-MoE, SwapMoE, eMoE, Edge0 prerouter,
DAOP approximate precompute, PowerInfer neuron skipping, verifier "expert
budgets" in some spec-decode papers, FP8 dense weights and FP8 KV as defaults
(both in FreeToken production today), ds4's opportunistic MTP acceptance.

### Design consequences (things that should change or be decided now)

1. **The FreeToken baseline is partly bought with rule-breaking precision.**
   Production halves dense bytes per step (7.8 to 3.9 GiB) with FP8 dense
   weights (+12 to 24% decode) and uses an FP8 KV cache. oominf keeps bf16 dense
   weights, so its dense floor is about 9 to 10 ms/token vs FreeToken's about 5.
   Either the success criterion should compare against FreeToken with bf16
   dense (and bf16 KV), or the plan must assume oominf needs speculative
   decoding (rank 4) to match. Recommend: record both baselines in the A/B.
2. **KV precision default conflicts between the brief and #6825.** The brief
   says fp32 KV by default; #6825 says bf16. In this regime KV VRAM is expert
   slots: Qwen 3.8 Flash full-attention KV is about 49 KB/token in fp32 (12
   layers x 2 KV heads x 256 x K,V), so 100k context is about 4.9 GB fp32 vs
   2.5 GB bf16, which is about 870 expert slots (about 18 per layer, roughly
   +0.4 to 0.8 VRAM misses per layer per token at that capacity). bf16 KV
   qualifies under "rounding we benefit from" only if it passes the parity
   gate; decide one default explicitly.
3. **RAM must be a first-class expert tier, not page cache.** FreeToken lived
   on mmap + WILLNEED + page cache and spent much of its history fighting
   faults, readahead and page-cache squeeze (section 4, rows on UFFD, THP,
   read_ahead_kb, host reserve). The PLE tables (51 GB) and the expert cache
   compete for the same 61 GB; budget them explicitly in one allocator, with
   O_DIRECT for both.
4. **The CUDA-graph slot model in `specs/ExpertTiering.tla` should gain an
   indirection mode.** Today a captured graph bakes slot addresses, so those
   slots are pinned while the graph is replayable. A graph that bakes only the
   address of a device-side slot table, which kernels read at run time, lets
   one graph serve every routing. The invariant becomes "a table entry the
   kernel can read points at a ready slot until that kernel completes", which
   is the existing routed-launch rule applied per layer. Worth adding before
   the decode loop is built (rank 3).
5. **Speculative decoding needs a parity policy decision.** A K-token verify
   is numerically a short prefill; FreeToken found batched verify changed
   results vs width-1 decode (reduction order), so greedy outputs with and
   without speculation can differ at near-ties even though both are within
   reference tolerance. Decide whether the gate is "within reference budget"
   (spec decode allowed) or "bitwise equal to non-speculative decode" (needs
   batch-invariant verify kernels).
6. **MTP experts are bf16 and large** (5.2 GB for the MTP layer). Plan their
   placement (host RAM plus CPU execution, or a hot VRAM subset) as part of the
   tier budget; FreeToken's MTP v1 failed largely on streaming these per draft.

## 2. Our own evidence

### 2.1 Cache-policy replay on the recorded trace

Simulators replayed `trace-routedump1` per layer (256 warm-up steps, then
misses counted over the remaining 3584 steps; policies and capacities per
layer). LRU, LFU (in-trace counts, with and without halving every 128 steps)
and Belady's offline optimum (OPT), averaged over 8 layers (every 6th):

| Slots per layer | LRU | LFU | LFU, decay 128 | OPT (unreachable bound) |
|---|---|---|---|---|
| 64 | 4.25 | 2.88 | 2.94 | 1.83 |
| 96 | 2.37 | 1.66 | 1.61 | 0.92 |
| 128 | 1.09 | 0.97 | 0.95 | 0.50 |
| 192 | 0.49 | 0.38 | 0.39 | 0.20 |
| 256 | 0.20 | 0.15 | 0.17 | 0.08 |

(Misses per layer per token. LRU-128 here is 1.09 vs 0.63 in
`docs/iobench-results.md` because the bench timed a 48-step window; this
averages the whole trace.)

Other facts from the same trace:

- Static top-N per layer by whole-trace frequency covers 72.1% (N=64), 89.3%
  (128), 98.5% (256), 99.9% (384) of activations. Only 367 of 512 experts per
  layer appear at all (minimum 244). Routing is strongly skewed per workload.
- One global LRU pool of the same total size is no better than per-layer LRU
  (4.11 vs about 4.25 at 64/layer, 1.09 vs about 1.1 at 128/layer).
- Speculative verify union (actual consecutive tokens): k=1, 2, 3, 4, 5 tokens
  touch 10.0, 17.5, 24.2, 30.2, 35.9 distinct experts per layer (10.0, 8.7,
  8.1, 7.6, 7.2 per token). Misses per accepted token under LRU-128 stay at
  1.14 for every k: speculation neither adds nor saves cache misses for
  accepted tokens; only rejected drafts add misses.

Caveats: one trace, one workload (a long continuation); LFU counts are learned
on the same trace it is scored on (optimistic); OPT is an oracle. A 2026
reproducibility study (Zhang, arXiv 2608.07911) warns that sloppy replay
inflates recency policies by 27 to 29% and that the OPT gap overstates what
practical policies recover. Treat the LFU gain as "about 10 to 30%, verify on
more traces".

### 2.2 Implications for tier sizing

With about 85 VRAM slots per layer, VRAM misses are about 2.4 to 2.9 per layer
(LRU) or about 1.6 to 1.7 (LFU-ish), so 80 to 140 host-tier experts per token.
Moving them all over PCIe costs about 10 to 17 ms/token; running them on the
CPU at 30 to 60 GB/s costs about 4 to 13 ms but overlaps with GPU work. With a
230 to 290 slot/layer RAM tier, disk misses are about 0.1 to 0.3 per layer, so
5 to 15 per token, mostly overlappable with lookahead. This is why ranks 1 and
2 come before anything else.

## 3. Per-idea sections

### 3.1 Frequency-aware tiered expert cache (rank 1)

- **What:** per-record residency in VRAM slots and a pinned host arena, with a
  recency plus frequency policy (LFU with decay, LRFU, or ARC-like), experts in
  the current top-k protected, and a startup seed from a persisted hot plan.
- **Evidence:** section 2.1 (LFU about 10 to 30% fewer misses than LRU on our
  trace). SGLang SSD Expert Pack (LMSYS blog, 2026-08-29) uses a
  frequency+recency VRAM cache and protects current top-k; FlashMoE (arXiv
  2601.17063, [abs-only]) reports up to 51% better hit rate than LRU/LFU with a
  learned recency/frequency blend; MoE-Infinity (2401.14361) uses request-level
  activation matrices for eviction and prefetch at batch 1; "In-depth analysis
  of caching and prefetching" (2511.05814, [abs-only]) reports LFU beats LRU.
  FreeToken: expert-granular HOT residency x1 19.5 to 23.1 tok/s; online
  hot-set adaptation pair rate 62.6 to 73.3%; split prefill/decode histories
  +13% post-document decode; hot plan persistence first request 267 to 351
  tok/s. llama.cpp PR #25294 (open) uses decaying hotness + LRU; issue #20757
  proposes VRAM/pinned RAM/SSD with SLRU.
- **Counter-evidence:** Flash-MoE found the OS page cache beat its custom LRU
  by 38% (on Apple unified memory, 4-of-10 experts). The kernel-tiering study
  (2608.12103) found oracle static pinning only 1.09 to 1.11x over the page
  cache on a trillion-parameter trace. Local-routing-consistency work
  (2505.16056, ICLR 2026) says shared-expert models are less cache-friendly;
  ours has a shared expert, but our measured hit rates are already high.
- **Impact:** at 64 to 96 VRAM slots/layer, 0.7 to 1.4 fewer host-tier misses
  per layer per token (35 to 65 per token). Separate prefill and decode
  frequency histories (FreeToken lesson) so long prompts do not flush the
  decode working set.
- **Rules:** lossless.
- **Maps to:** expert storage tiers (host arena, VRAM slots), placement policy
  (scoring, protection, seeding). Policy must be a trait with a replay harness
  over recorded traces, so policies are compared offline before A/B.
- **Cost:** medium. The replay simulator and trace format already exist
  (`oominf-iobench`).

### 3.2 CPU execution of host-tier misses (rank 2)

- **What:** when an expert is in host RAM but not VRAM, either copy it to a
  VRAM slot (about 0.12 ms of PCIe) or compute it on the CPU from RAM (about
  0.05 to 0.09 ms of DDR5 bandwidth at bs=1). Split each layer's misses so CPU
  and copy paths finish together; GPU computes its hits meanwhile.
- **Evidence:** Fiddler (ICLR 2025, 2402.07033) per-expert CPU-vs-transfer
  choice; HybriMoE (DAC 2025, 2504.05897) dynamic intra-layer split, 1.70x
  decode over prior hybrids; KTransformers (SOSP 2025) runs routed experts on
  CPU with hot experts on GPU and schedules CPU work asynchronously so it does
  not break CUDA graphs (launch overhead "from over 20% to nearly zero", LMSYS
  2025-10-22); llama.cpp `--n-cpu-moe` decode tracks RAM bandwidth almost
  linearly (gpt-oss-120b on DDR5-6000: 25 to 28 tok/s, about 10 tok/s when RAM
  ran at 2000 MT/s). FreeToken: hybrid pinned-layer CPU misses with fetch cap
  2 cut PCIe gather 7.4 to 3.5 ms/token, decode +9%; 8 CPU MoE workers took
  the step from 55 to 44 ms; but its CPU kernel reached only about 30 GB/s of a
  60 to 70 GB/s ceiling, and 10.7 ms/step went to page-fault tails.
- **AVX-512 vs AMX:** KTransformers' AMX path is for high-intensity prefill;
  its decode kernels (BF16, FP8) need AVX512F+BW+BF16(+VBMI), which the
  7800X3D has. No AMX is needed for decode. **[inferred from their listed
  requirements, not tested]**
- **Exact NVFP4 on AVX-512:** E2M1 has 1 mantissa bit and E4M3 has 3, so each
  weight times its block scale is exact in fp32 (and in bf16). Decode via
  `vpermb` (VBMI) nibble lookup, scale via a 256-entry E4M3 table, fp32 FMA
  against fp32 activations, apply `weight_scale_2` after accumulation. At bs=1
  the math is free; the kernel only needs to stream RAM near 60 GB/s. Output
  equals the GPU path up to summation order, which the per-op fixtures already
  tolerate.
- **Why it beats FreeToken's version:** a Rust worker pool with no page faults
  (records in a pre-faulted, mlock'd arena), no Python fences, and a kernel
  written for bandwidth should approach 2x FreeToken's achieved CPU throughput.
  **[estimate]**
- **Impact:** with 80 to 140 host-tier misses/token, a balanced split hides
  most of a 10 to 17 ms PCIe cost; expect 5 to 10 ms/token saved vs copy-only.
- **Rules:** lossless.
- **Maps to:** device backend (a CPU "device" implementing the same MoE op for
  a subset of experts) and placement policy (cost model per miss). Keeps the
  backend rule: the core asks "MoE for layer L, these experts", the backend
  partitions.
- **Cost:** medium-high: AVX-512 kernel with fixtures, worker pool pinned to
  cores, cost model, and synchronisation with the GPU stream (rank 3).

### 3.3 CUDA graphs with expert slot tables (rank 3)

- **What:** capture the whole decode step once. Routed MoE kernels read a
  device-side table `expert -> slot pointer` (or "computed on CPU, add this
  host-written partial") instead of baked addresses. Per layer: the router
  writes top-k to mapped host memory, the graph waits on a flag
  (`cuStreamWaitValue32`, capturable), a host thread resolves misses (CPU
  compute, H2D into free slots), fills the table and releases the flag.
- **Evidence:** vLLM FULL_AND_PIECEWISE and SGLang piecewise graphs split at
  data-dependent points (vLLM docs 2026-09-26; SGLang docs); KTransformers'
  async CPU scheduling inside graphs; FreeToken used stream memops for its
  handshake and measured a host-func handshake slower (14.8 to 18.5 vs 16.3 to
  19.9 tok/s); FreeToken spin/park/fork-join variants were all neutral, the
  real cost was page faults. FreeToken roofline (2026-10-01): GPU idle about
  56% of a step waiting on CPU.
- **Impact:** removes per-layer launch latency (48 layers x many kernels) and
  makes CPU expert work overlap GPU hits. Several ms/token; exact share
  unknown until the forward pass is profiled. **[estimate]**
- **Rules:** lossless.
- **Maps to:** core decode loop, device backend (graph capture is CUDA-only;
  the trait must allow backends without graphs), tiering spec (see design
  consequence 4).
- **Cost:** medium-high; mostly protocol work and the TLA+ update.

### 3.4 Exact MTP speculative decoding (rank 4)

- **What:** the checkpoint's MTP block drafts K tokens; the main model verifies
  K+1 positions in one pass; standard (exact) rejection sampling keeps the
  output distribution; greedy keeps exact argmax.
- **Evidence:** SGLang DeepSeek V3 MTP 1.8x at bs=1 (H200); llama.cpp MTP
  merged 2026-05-16 (PR #22673), about 75% acceptance with 3 drafts on Qwen3.6;
  ds4 ships Qwen 3.8 Flash MTP with a chained second draft, but its default
  "opportunistic" mode does not preserve the sampling distribution
  (`--mtp-exact-sampling` does; docs/SPECULATIVE_DECODING.md, no acceptance
  numbers published). Offload-aware SD: SpecMoEOff (2508.21706, up to 2.5x,
  [abs-only]), SP-MoE (2510.10302, 1.07 to 3.5x, [abs-only]), Cascade
  (2506.20675: static SD can slow MoE up to 1.5x; utility-gated K caps slowdown
  at 5%), EcoSpec (2607.12696, [abs-only]) prefers drafts whose experts are
  cached while keeping the target verification rule. vLLM warns DeepSeek's
  single-layer MTP quality is "not effectively guaranteed" above K=1.
- **FreeToken history:** MTP K=1 eager 12.6 vs 18.0 tok/s (drafted token's
  experts went through the CPU tier at about 2x cost); with graphs 19.5 vs
  21.5 tok/s off at 72% acceptance (break-even about 78%); on the current
  profile -32 to -52% decode, and an FP8 lm_head zeroed acceptance; batched K=3
  abandoned on exact GDN rollback; BF16 MTP experts streamed per draft killed
  MTP v1.
- **Why the reasons may not hold:** (a) on the GPU-hit path a verify pass
  reads each weight once for K+1 tokens; our trace shows the expert union grows
  sublinearly (17.5 for 2 tokens) and misses per accepted token do not rise;
  (b) the dense floor (about 9 to 10 ms bf16) is amortised, and oominf has a
  bigger dense floor than FreeToken because it will not use FP8 dense; (c) GDN
  rollback in Rust can snapshot state per draft position (36 layers x 48 heads
  x 128 x 128 fp32 is about 113 MB per position) or replay recurrence inputs
  (SGLang's "ReplaySSM" for Qwen3.8, LMSYS 2026-08-12 **[unverified detail]**);
  (d) the CPU-tier cost scales with the union, not per token, once CPU experts
  run batched over the K+1 rows.
- **Why it might still lose:** the MTP block itself has 10 bf16 experts per
  draft (52 MB, about 1 ms on CPU, more if not resident); rejected drafts add
  misses; at near-ties batched verify numerics differ from width-1 decode
  (FreeToken bench/4090-target-verify-cost branches).
- **Impact estimate:** warm step about 20 ms single-token; K=1 pass about 25 to
  28 ms at 70% acceptance (1.7 tokens) gives about 15 to 16 ms/token, about
  1.2 to 1.35x. **[estimate from the budget model; must be measured]**
- **Rules:** lossless only with exact acceptance. Never adopt opportunistic
  acceptance or verifier expert budgets. Parity policy: see design consequence
  5. A batch-invariant verify kernel option (same reduction order as width-1)
  would make "spec on" bitwise equal to "spec off".
- **Maps to:** core decode loop (draft/verify/rollback), model trait (MTP head,
  state snapshot/restore), placement policy (MTP experts tier), scheduler.
- **Cost:** high. Do it after ranks 1 to 3, and gate K by measured utility.

### 3.5 Layer-major streaming prefill (rank 5)

- **What:** for long prompts, process a large token group layer by layer so
  each expert record is read once per group (all 512 experts per layer are
  usually hit), double-buffer the next layer's records from disk/host while
  computing the current one, and keep the decode working set from being
  flushed.
- **Evidence:** FreeToken layer-major prefill plus parallel/predicted/cached
  DISK staging took 100k cold TTFT 93.7 to 53.2 s; v2 NVFP4 prefill kernel 45.5
  to 29.5 s; 64k groups 22.7 to 21.9 s. KTransformers layerwise prefill above a
  token threshold; llama.cpp copies CPU weights to GPU for batches of 32+
  tokens; DuoServe-MoE (2509.07379) separates prefill streaming from decode
  caching; llama.cpp PR #25294 "wave-partitioned" prefill, 5.3x prefill.
- **Impact:** all experts once is 68 GB, about 9 to 10 s from disk at 7.4 GB/s
  sequential, less with the host tier; this bounds long-prompt TTFT from below.
- **Rules:** lossless.
- **Maps to:** prefill strategy. **Cost:** medium.

### 3.6 Disk prefix cache with GDN snapshots (rank 6)

- **What:** radix/paged prefix cache for the 12 full-attention layers' KV
  (plus indexer keys) and GDN conv/recurrent state snapshots at block
  boundaries, in VRAM, host and disk tiers.
- **Evidence:** FreeToken disk prefix cache: 10k agent cold TTFT 870 s to 14.8
  s; harness-root restore 22.1 to 7.05 s. SGLang HiCache (GPU/host/storage
  tiers, up to 80% lower TTFT) and Unified Radix Cache (2026-08-11): MAMBA
  state is valid only at exact checkpoints and is copied before mutation. vLLM
  `mamba_cache_mode="align"` checkpoints at block boundaries; `"all"` composes
  with MTP (PR #50172). FreeToken lessons: pinned staging leaked through the
  torch caching allocator (fixed by pageable staging: cache-on 100k cold 130 to
  90.6 s); FADV_DONTNEED and transient registration were reverted.
- **Rules:** lossless if snapshots are exact copies at the dtype in use.
- **Maps to:** prefix cache backend (TLA+ invariants already listed in #6825).
- **Cost:** medium. GDN snapshot size is small (about 113 MB plus conv state).

### 3.7 Router-driven lookahead and multi-layer read batching (rank 7)

- **What:** after layer L's attention, apply layer L+1's (and L+2's) real
  router to the current hidden state, and issue disk-to-host reads for
  predicted experts that are on disk only, into a **separate staging pool** that
  does not displace cached experts; promote on use, drop otherwise. Keep up to 4
  layers of reads in flight (measured 7.0 to 7.4 vs 5.6 to 6.6 GB/s).
- **Evidence:** Mixtral-offloading (2312.17238) next-layer gate, about 60 to
  70% recall for 1 to 2 experts **[read off figures]**; Fate (2502.12224) high
  accuracy in deep layers, about 74% shallow (metric filtered to high-weight
  experts); ProMoE (2410.22134) on an RTX 4090: learned predictor 84.7%,
  decode 2.07x vs other offloaders; pre-attention linear predictor
  (2511.10676) 93 to 97.6%. Against: FreeToken next-layer router covered only
  42.8% at F=8 and lookahead WILLNEED at 48% accuracy doubled traffic;
  Flash-MoE's temporal prediction (-18%) and speculative routing (-38%) slowed
  it down; kernel-tiering (2608.12103) found perfect one-layer lookahead worth
  about 5% on top of a recency cache.
- **Judgement:** worth it only for the disk tier, and only into staging. Wasted
  predictions then cost idle NVMe bandwidth, not cache contents, which was
  FreeToken's failure mode. Host-to-VRAM speculative copies compete with real
  misses on PCIe; skip them unless measurement shows idle PCIe.
- **Rules:** lossless (hint only). A trained predictor is allowed too, as long
  as it only drives loads.
- **Maps to:** placement policy (predictor), storage tiers (staging pool).
- **Cost:** low-medium.

### 3.8 Pinned host arena and DMA overlap (rank 8)

- **What:** one `cuMemHostRegister` (or `cuMemHostAlloc`) of the whole host
  expert arena at startup, record-sized slots, mlock'd and pre-faulted;
  O_DIRECT reads land directly in it; H2D on a dedicated copy stream with
  events; no transient registration.
- **Evidence:** FreeToken transient `cudaHostRegister` of 1.32 GiB took 0.5 to
  1.9 s (reverted); torch pinned-allocator retention caused the prefix-cache
  regression; SGLang Expert Pack reads O_DIRECT into aligned pinned buffers
  with overlapping per-expert H2D. GPUDirect Storage style SSD-to-GPU (Endor,
  2406.11674) is not worth it on consumer GeForce. **[GDS support on GeForce
  not re-verified]**
- **Rules:** lossless. **Maps to:** storage tiers. **Cost:** low.

### 3.9 Other ideas considered

| Idea | Source | Verdict |
|---|---|---|
| EPLB / redundant experts | vLLM `--enable-eplb`, SGLang expert-distribution recorder | Multi-GPU load balancing does not apply. The record-then-place workflow does: persist a routing profile and seed the hot set from it (FreeToken hot plan persistence). Folded into rank 1. |
| Chunked prefill | vLLM, SGLang | Only matters with concurrent streams; for 1 to 2 streams, layer-major groups (rank 5) dominate. Keep chunking for the second stream's decode latency. Low priority. |
| N-gram / prompt-lookup / suffix speculation | vLLM, SGLang NGRAM, FreeToken branch | Lossless and nearly free to draft, but FreeToken saw wins only on repetition and losses on multi-turn. Revisit as a second draft source under the same utility gate as MTP. Low priority. |
| EAGLE-3 draft heads | vLLM, SGLang | Needs a trained head we do not ship; MTP is already in the checkpoint. Skip for Qwen; reconsider for models without MTP. |
| Kernel autotuning on device | Magnitude (Rust/TypeScript engine, launched 2026-09-30, claims up to 2x llama.cpp; MoE offload is roadmap only) | Cheap idea for tile sizes per op on sm_89; low priority. |
| Expert Deferral | KTransformers SOSP 2025 | Out: changes outputs (up to 0.5% accuracy drop). |
| Low-precision substitute on miss | HOBBIT, EdgeMoE | Out. |
| Router replacement / pruning / budgets | SiDA, AdapMoE, SwapMoE, eMoE, Edge0, verifier expert budgets | Out. |
| Neuron-level hot/cold (PowerInfer) | PowerInfer, PowerInfer-2 | Neuron skipping is lossy for SwiGLU; the expert-level version is ranks 1 and 2. |
| Re-quantised experts (INT4 AutoRound) | #6825 open question | Only as a separate layout tag that passes the parity gate; out of scope here. |
| Energy | 2508.06978 | SSD offload costs about 3x the energy per token of RAM offload; another reason to make RAM the main tier. |

## 4. Dismissed in the FreeToken era: still dismissed or worth revisiting

Categories: Py = Python/torch overhead, FT = FreeToken design, HW = hardware,
Q = quality.

| Idea | FreeToken source | Stated reason | Cause | Verdict for oominf |
|---|---|---|---|---|
| MTP K=1 eager | `52d737a`, RESULTS.md | 12.6 vs 18.0 tok/s; draft experts via CPU tier at about 2x cost | FT+HW | **Revisit** (rank 4): batched verify over a union, bf16 dense floor to amortise, graphs from day one |
| MTP with verify graphs | `feat/mtp-graphs`, blog-diff 2.1 | 19.5 vs 21.5 tok/s off at 72% acceptance; break-even about 78% | FT+HW | **Revisit**: break-even depends on dense floor; ours is about 2x FreeToken's FP8 floor |
| MTP on current profile | ab-mtp-2 | -32 to -52% decode; FP8 lm_head zeroed acceptance | FT, Q | **Revisit**; the FP8 lm_head cause does not apply (we keep bf16) |
| MTP batched K=3 + draft KV | `bench/freetoken-mtp-batched-spec.md` | abandoned; exact GDN rollback hardest | FT | **Revisit** with per-position snapshots or input replay |
| MTP v1 on L4 | RESULTS.md | 0.20 tok/s, BF16 MTP experts streamed per draft | FT+HW | **Still a risk**: plan MTP expert placement explicitly |
| N-gram speculation | `perf/4090-ngram-serving` | wins on repetition, loses on multi-turn | Py + acceptance | **Low-priority revisit** under a utility gate |
| Multi-token verify exactness | `bench/4090-target-verify-cost` etc. | batched GEMM reduction order changed results | Q | **Still a real issue**; needs the parity policy decision or batch-invariant kernels |
| Paired NVFP4 CPU dot | `perf/4090-nvfp4-pair-dot` | bit-exact, no gain at bs=1 | HW | Still dismissed for 1 stream; reconsider for verify batches (K+1 rows share weights) |
| Decode weight-unpack reuse | `perf/4090-decode-weight-reuse` | gains only when routes share experts | HW | Same: useful only for spec verify and 2-stream decode |
| GPU-fetch decode for DISK layers | `affc25c`, law 5 | about 990 fills/step of 2.6 MB, slot thrash on 24 GB | HW | **Still dismissed as all-fetch**; the hybrid split (rank 2) is the answer |
| Hybrid PCIe fetch (Aug) / cold-fetch N | RESULTS.md | lost to CPU offload | HW | **Revisited already** (fetch cap 2 won later); oominf should make the split a cost model, not a constant |
| One-step lookahead WILLNEED | `46436ce` | 48% accuracy doubled prefetch traffic | FT | **Revisit narrowly** (rank 7): disk tier only, separate staging, no cache pollution |
| Pre-gating next-layer cold experts | `ca83b43`, `perf/disk-pregate` | 42.8% coverage at F=8; 28 MB/layer fetch exceeds a 0.9 ms layer; WILLNEED null and cost 10 to 30 ms/step of advice CPU | HW+FT | **Mostly still dismissed** for host-to-VRAM; the advice-CPU cost was madvise-specific and goes away with io_uring |
| UFFD pager | `894f07f` | below madvise throughput | FT/HW | **Moot**: no page-cache tier in oominf |
| HMM PLE backend | RESULTS.md | UVM kernel oopses | HW/driver | Still dismissed; use io_uring row gathers (FreeToken's winner) |
| THP on expert banks | `f36169f` | neutral to -28% prefill | HW | Moot with an explicitly allocated arena; use 2 MB pages for the arena only if measured |
| KV ladder / shrink KV for HOT | `057ce31`, `perf/kv-vram-for-experts` | 107 s rebuild on growth; +8.6% only | FT | **Revisit** as a static, startup-time KV vs slot split chosen from max context, no runtime rebuild |
| Larger HOT budget | ab-fp8-budget-1 | faults +38% from page-cache squeeze | HW (61 GB) | **Revisit**: the squeeze came from page cache; an explicit arena changes the trade |
| Pinned-layer hot set / unequal per-layer capacity | chain 17, `33b411c` | no gain; routing near uniform across layers | FT / model | Still dismissed as a first-order lever; let a global scorer allocate if cheap |
| Empty-skip of all-hot layers | `780513b` | under 1 ms saved | FT | Still dismissed |
| Spin executor / thread caps / barriers | `feat/spinwait`, `perf/cpu-moe-stall` | neutral; real cost was page faults | HW | Mostly still dismissed; re-measure once faults are gone, since the fault tail hid everything else |
| Host-func handshake | RESULTS.md | slower than memops | Py/CUDA | Still dismissed; use memops (rank 3) |
| Cross-wave CPU/GPU pipelining | `43ba7f4` | needed breaking one-forward-in-flight | FT | **Revisit**: per-layer handshake in one graph gives intra-step overlap without that |
| Concurrency x12/x16, 4 vs 1 | RESULTS.md, `perf/4090-concurrent-wall-client` | VRAM; latency 20.8 to 82.1 s | HW | Still dismissed; we target 1 to 2 streams |
| Whole-layer copy prefill / populate-then-copy | `perf/4090-disk-copy`, `f1ab9e9` | far slower than CPU prefill for short prompts | HW | Still dismissed for short prompts; layer streaming is right for long ones (rank 5) |
| Direct-only O_DIRECT prefill IO | `perf/4090-direct-prefill-io` | +1.3 to 4.8% slower: lost page-cache hits | HW | **Revisit**: with our own host tier, O_DIRECT loses nothing (iobench: io_uring O_DIRECT beats pread 5 to 20%) |
| Buffered HOT staging IO | `perf/4090-hot-staging-io` | +19.6% vs mmap | HW/OS | Moot |
| Eager per-layer prefill page release | `e0d5b04` | consecutive chunks share experts | FT | Still dismissed; the lesson (do not drop what the next chunk needs) stands |
| Whole-layer streaming in layer-major groups | `4cbd849` reverted | 22 to 29 GiB from NVMe per 32k vs 4 to 9; routing skewed | FT/HW | **Partly revisit**: stream only the experts the group routes to, from host first, disk second |
| Cross-chunk expert GEMM batching | `6631a9d` | no gain with v2 kernel; 16k OOM | HW | Still dismissed |
| Fused SwiGLU epilogue | `562458a` | bit-identical, 1.5 to 2.3% slower | HW | Re-measure in our own kernels; cheap |
| FP8 MMA prefill kernel | prefill-depth "Kernel probes" | 5.5% relative RMS error | Q | **Still dismissed** (violates precision rule) |
| Prefill swap caps, adaptation cadences, decode-focused planning, idle ticks | various, reverted | trade cold TTFT vs repeat and decode | FT | Folded into rank 1: separate prefill and decode histories, tune by trace replay |
| Larger chunks before governor fix | prefill-depth | VRAM accounting, then OOM | FT then HW | Partly moot: a Rust allocator with exact accounting removes the governor error; VRAM ceiling remains |
| 512-token chunks | RESULTS.md | 16x re-reads | HW | Still dismissed |
| FADV_DONTNEED prefix entries / transient registration | `842049d`, `e0e0c13` (reverted) | not the cause / registration cost | Py/CUDA | Moot with one startup-registered arena |
| e2m1 PLE table on L4 | RESULTS.md | miss-install cost | FT | Moot; PLE stays as released (fp8) |
| CPU executor on a 176 GB box | blog-diff 5.2 | PCIe beat 48 vCPU when RAM is ample | HW | Note: the CPU/GPU split must be a measured cost model, since the answer flips with RAM:model ratio (relevant for other hosts) |
| Network disk tier | blog-diff 5.2 | 5,700 IOPS | HW | Still dismissed |
| FP8 dense weights, FP8 KV (adopted in FreeToken) | #117, #121; profile default | +12 to 24% decode | Q | **Out by our rules** (re-quantised weights; KV only as an opt-in that passes parity) |

## 5. Notes on the surveyed engines

- **ds4 (antirez/ds4, "DwarfStar 4")**: a C engine that began as Metal-only
  DeepSeek V4 Flash and now covers Qwen 3.8 Flash Next and GLM 5.3 Flash. It
  keeps the n-gram table on disk and reads selected rows directly; streams
  routed experts from SSD into a bounded cache (I/O mechanism not documented);
  supports MTP with an adaptive chained second draft. Its model files are Q2/Q4
  requantisations, and its default MTP mode is not exact, so it is design prior
  art only. No RTX 4090 numbers. "ds4" in the brief is this project; it is
  named after DeepSeek V4 (released 2026-04-24, MIT; 284B/13B-active Flash,
  FP4 routed experts, mHC hyper-connections, single-layer MTP, compressed and
  sparse attention).
- **Magnitude (magnitudedev/magnitude)**: an agent-oriented local inference
  engine that compiles and autotunes kernels on the device (HN launch
  2026-09-30; v0.2.5 on 2026-10-03 added MTP for MoE models). The repo surface
  is TypeScript; the HN thread says the engine and kernel runtime are Rust
  **[unverified beyond the thread]**. No MoE offload yet (roadmap), no
  connection to ds4 found beyond HN commenters comparing them.
- **SGLang SSD Expert Pack** (LMSYS 2026-08-29): closest published analogue.
  Contiguous per-(layer, expert) records, O_DIRECT into pinned buffers,
  frequency+recency VRAM cache, explicit "Top-K is unchanged; not replaced,
  pruned, skipped, or merged". RTX 5090 + 32 GB RAM; host RAM is only staging,
  not a cache tier. DeepSeek V4 Flash about 2 tok/s at 46 to 54% VRAM hit rate.
  Our 61 GB RAM tier is the main structural advantage over it.
- **SSD-LLaMA** (2609.18110, [abs-only]): three-tier SSD/RAM/VRAM, balanced
  CPU-GPU execution, executes every selected expert. Closest paper; read in
  full before building rank 2.
- **llama.cpp**: `-ot`, `--cpu-moe`, `--n-cpu-moe`; mmap beyond RAM thrashes
  (DeepSeek R1 212 GB on 96 GB: 1.45 tok/s, disk at 2 to 5 GB/s of 12.4). Open
  PR #25294 streams experts with O_DIRECT and a hotness+LRU slot cache. MTP
  merged (PR #22673).
- **KTransformers**: CPU-routed experts with hot GPU experts, async CPU tasks
  inside CUDA graphs, layerwise prefill, dynamic expert update from prefill
  stats. Its GLM 5.3 Flash path needs 350 GB RAM. Exclude Expert Deferral.
- **vLLM**: graph modes, EPLB, `mamba_cache_mode`, speculative methods; PR
  #57943 adds expert-granular host/staging banks **[unverified status]**.

## 6. Sources

Engines and blogs:
- antirez/ds4: https://github.com/antirez/ds4 (docs/MODELS.md, docs/QWEN38_FLASH_NEXT.md, docs/SSD_STREAMING.md, docs/SPECULATIVE_DECODING.md)
- magnitudedev/magnitude: https://github.com/magnitudedev/magnitude ; HN launch https://news.ycombinator.com/item?id=49911995 ; releases https://github.com/magnitudedev/magnitude/releases
- DeepSeek V4 on SGLang: https://www.lmsys.org/blog/2026-04-25-deepseek-v4/ ; paper https://arxiv.org/pdf/2606.19348
- SGLang SSD Expert Pack: https://www.lmsys.org/blog/2026-08-29-sglang-ssd-expert-pack
- SGLang HiCache: https://www.lmsys.org/blog/2025-09-10-sglang-hicache/
- SGLang Unified Radix Cache: https://www.lmsys.org/blog/2026-08-11-unified-radix-cache/
- SGLang Qwen3.8 day-0 (ReplaySSM): https://www.lmsys.org/blog/2026-08-12-qwen3-8-day0-support
- SGLang + KTransformers: https://www.lmsys.org/blog/2025-10-22-KTransformers/
- SGLang piecewise CUDA graphs: https://docs.sglang.io/advanced_features/piecewise_cuda_graph.html ; speculative decoding: https://docs.sglang.ai/advanced_features/speculative_decoding.html
- vLLM CUDA graphs: https://docs.vllm.ai/en/latest/design/cuda_graphs.html ; EP/EPLB: https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment.html ; speculative decoding: https://docs.vllm.ai/en/latest/features/speculative_decoding/ ; PR #50172 https://github.com/vllm-project/vllm/pull/50172
- KTransformers: https://github.com/kvcache-ai/ktransformers ; kt-kernel: https://kvcache-ai.github.io/ktransformers/en/kt-kernel/kt-kernel_intro.html ; SOSP 2025 paper https://dl.acm.org/doi/pdf/10.1145/3731569.3764843 ; Expert Deferral PR https://github.com/sgl-project/sglang/pull/12586
- llama.cpp: MTP PR https://github.com/ggml-org/llama.cpp/pull/22673 ; SSD streaming PR https://github.com/ggml-org/llama.cpp/pull/25294 ; tier issue https://github.com/ggml-org/llama.cpp/issues/20757 ; offload guide https://gist.github.com/DocShotgun/a02a4c0c0a57e43ff4f038b46ca66ae0 ; gpt-oss-120b DDR5 numbers https://carteakey.dev/blog/local-inference/optimizing-gpt-oss-120b-local-inference/ ; DeepSeek R1 from NVMe https://huggingface.co/unsloth/DeepSeek-R1-GGUF/discussions/13
- Flash-MoE: https://github.com/danveloper/flash-moe

Papers (arXiv ids):
- MoE-Infinity 2401.14361; Pre-gated MoE 2308.12066 (ISCA 2024); ExpertFlow 2410.17954; Fiddler 2402.07033 (ICLR 2025); MoE-Lightning 2411.11217; HOBBIT 2411.01433; AdapMoE 2408.10284; ProMoE 2410.22134; SiDA-MoE 2310.18859 (MLSys 2024); Mixtral-offloading 2312.17238; EdgeMoE 2308.14352; SwapMoE 2308.15030 (ACL 2024); Lina 2210.17223 (ATC 2023); MoE-Gen 2503.09716; fMoE/FineMoE 2502.05370; DAOP 2501.10375; Klotski 2502.06888; MoE-Lens 2504.09345; HybriMoE 2504.05897 (DAC 2025); MoE-Beyond 2508.17137; DuoServe-MoE 2509.07379; eMoE 2503.06823; PowerInfer 2312.12456 (SOSP 2024); PowerInfer-2 2406.06282; LLM in a flash 2312.11514 (ACL 2024)
- SSD and tiering: FlashMoE 2601.17063; SSD-LLaMA 2609.18110; kernel-managed tiering 2608.12103; Endor 2406.11674; SSD energy 2508.06978; Edge0 2609.18063
- Prediction and caching: Fate 2502.12224; pre-attention prediction 2511.10676; SpecPrefetch 2607.24787; replacement reproducibility 2608.07911; SeqMoE 2609.12978; caching/prefetch analysis 2511.05814; DALI 2602.03495; local routing consistency 2505.16056 (ICLR 2026)
- Speculative decoding x MoE: SpecMoEOff 2508.21706; SP-MoE 2510.10302; MoESD 2505.19645; Cascade 2506.20675; EcoSpec 2607.12696; BigMoMo 2609.14643; MoE-SpAc 2603.09983; SpecMoE 2604.10152; DraftExpert 2607.24434

Could not verify: papers titled "DiskMoE" or "MoE from SSD" (none found); headline
numbers for fMoE, DuoServe, DALI and 2511.05814; exactness claims of FlashMoE,
SpecMoEOff, MoE-SpAc and SpecMoE beyond their abstracts.

FreeToken fork (read-only): `docs/4090-performance.md`,
`docs/prefill-depth-benchmark.md`, `docs/prefix-cache-page-pressure-20260926.md`,
`docs/disk-prefix-cache.md`, `bench/RESULTS.md`, `bench/blog-diff-doc.md`,
`bench/freetoken-mtp-batched-spec.md`, `scripts/serve-qwen-flash-4090.sh`, and
the commits and branches cited in section 4. Some branch results are recorded
only in node-4 A/B reports under `/var/lib/longhorn/nvme-02/freetoken/results/`.
