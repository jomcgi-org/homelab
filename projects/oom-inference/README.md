# oom-inference

A minimal, extensible inference engine for Mixture-of-Experts models larger than
GPU plus host memory, tuned for one or two interactive streams on one machine.
Routed experts are tiered across VRAM, pinned host memory and NVMe; weights are
used exactly as released. The CLI is `oominf`.

Apple Silicon support is in progress. The `oominf-metal` crate has tested GPU
primitives, shared expert transfers and macOS disk/memory support. The converter
accepts text-only Qwen3.5 MoE NVFP4 checkpoints, including a single safetensors
file. The CLI still runs inference through CUDA; full Qwen3.5 generation on
Metal is tracked in [#6896](https://github.com/jomcgi-org/homelab/issues/6896).

- [Architecture](docs/ARCHITECTURE.md): crates, interfaces, precision, tiers,
  and how to add a model or a platform.
- [Weight format](docs/FORMAT.md).
- [Testing](docs/TESTING.md): reference fixtures, gates and protocol specs.
- [Hardware](docs/HARDWARE.md): what runs where, measured on smaller simulated
  machines.

## Setup

### Prerequisites

- Linux on x86-64 with an NVIDIA GPU and its driver. Developed and measured on an
  RTX 4090 (24 GB). Kernels compile to PTX for `compute_89` by default; set
  `OOMINF_CUDA_ARCH` for another architecture.
- The CUDA toolkit (`nvcc`) to build the kernels (`NVCC` overrides its path). The
  CUDA libraries themselves are loaded at run time. Without `nvcc` everything still
  builds and the CPU tests run, but the CUDA backend refuses to start.
- The Rust toolchain pinned in `rust-toolchain.toml` (rustup installs it).
- Disk: about 135 GB for the converted Qwen 3.8 Flash checkpoint (dense 10 GB,
  experts 73 GB, tables 51 GB) on a local NVMe drive. Experts are read on demand
  while serving, so a slow drive slows cold prefill and decode misses directly;
  `oominf doctor` measures and warns (see [Hardware](docs/HARDWARE.md)).
- Memory: see the budget below. The tested minimum is in
  [Hardware](docs/HARDWARE.md).

### Build

The workspace builds with Cargo (it is excluded from Bazel via `.bazelignore`).

    cargo build --release          # target/release/oominf
    cargo test --workspace         # CPU tests; GPU tests are #[ignore]d (docs/TESTING.md)

### Checkpoint

Convert the upstream release checkpoint (sharded safetensors, e.g. the NVFP4
release of Qwen 3.8 Flash) once, then check every checksum:

    oominf convert --src <release checkpoint dir> --out model.oom
    oominf verify model.oom
    oominf inspect model.oom

### Memory budget

Everything but the routed experts lives on the GPU. The experts are cached in
VRAM (what is free once the model and a first sequence are loaded, less
`--vram-reserve-gib`, default 2) and in pinned host memory; the rest is read from
disk as needed. On every start `oominf` checks, fresh:

- usable host memory: the smaller of `MemAvailable` and the container's limit
  (cgroup v2 `memory.max`/`memory.high` or v1 `limit_in_bytes`, less what the
  group already uses);
- what the model keeps in host memory beside the tiers: host-placed KV caches
  (`--kv-placement host`), prefix checkpoints, prefix store snapshots
  (`--prefix-store-dir`), transfer buffers;
- `--host-reserve-gib` (default 10) left to the page cache, the OS and other
  processes.

The host tier gets the rest. A size given with `--host-expert-gib` or
`--vram-expert-gib` that does not fit is shrunk with a warning; when even the
smallest working tiers do not fit, a default reserve is lowered (to 3 GiB host,
1 GiB VRAM) with a warning, and only then does start-up fail, listing what is
needed. More memory means fewer disk reads: on a 64 GB machine the host tier
holds about 40 of the 68 GiB of experts.

`oominf doctor --model model.oom` prints what `serve` would decide on this
machine (the fresh checks, the hardware profile and the effective budgets)
without starting it.

### Smoke test

    oominf doctor --model model.oom
    oominf generate --model model.oom --prompt "What is the capital of France?" --max-tokens 16
    oominf serve --model model.oom &       # OpenAI and Anthropic APIs on 127.0.0.1:8091
    curl -s localhost:8091/health          # "ok" once loaded
    curl -s localhost:8091/v1/chat/completions -H 'content-type: application/json' \
      -d '{"messages": [{"role": "user", "content": "Say hi."}], "max_tokens": 16}'

The first `serve` on a machine measures the hardware for about a second and caches
the profile in `~/.cache/oominf/` (see `oominf tune`).

## Use

    oominf serve --model model.oom            # OpenAI and Anthropic APIs on :8091
    oominf generate --model model.oom --prompt "Hello"
    oominf bench --model model.oom            # decode speed and tier statistics
    oominf tune --model model.oom             # measure this machine, write the profile
    oominf doctor --model model.oom           # what serve would decide here

`serve` runs up to two requests at once by default (`--max-streams`); up to 16
more wait (`--max-queued`), and further requests get 429 with `Retry-After`.
Lossy speed modes (`--dense fp8`, `--expert-precision bf16`,
`--attention-precision bf16`) are off by default; see
[Precision](docs/ARCHITECTURE.md#precision).
