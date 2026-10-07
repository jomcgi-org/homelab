# oom-inference

A minimal, extensible inference engine for Mixture-of-Experts models larger than
GPU plus host memory, tuned for one or two interactive streams on one machine.
Routed experts are tiered across VRAM, pinned host memory and NVMe; weights are
used exactly as released. The CLI is `oominf`.

Apple Silicon runs Qwen3.5-35B-A3B text inference through direct Metal. The
released NVFP4 checkpoint is 23.59 GB; its converted text weights occupy 21.03 GB
and stream routed experts from SSD. On a 16 GiB M1 Pro, the optimized measured
run reached 2.8 to 2.9 generated tokens/sec with a 1 GiB expert cache and a
3.70 GiB peak process footprint.
See [Hardware](docs/HARDWARE.md#apple-silicon) for the command and limits.

- [Architecture](docs/ARCHITECTURE.md): crates, interfaces, precision, tiers,
  and how to add a model or a platform.
- [Weight format](docs/FORMAT.md).
- [Testing](docs/TESTING.md): reference fixtures, gates and protocol specs.
- [Hardware](docs/HARDWARE.md): what runs where, measured on smaller simulated
  machines.

## Apple Silicon

Build with the pinned Rust toolchain and `cargo build --release -p oominf`.
Convert a local Qwen3.5 MoE NVFP4 checkpoint, then verify it:

    target/release/oominf convert --src <checkpoint-dir> --out model.oom
    target/release/oominf verify model.oom
    target/release/oominf doctor --model model.oom
    target/release/oominf generate --model model.oom --no-thinking \
      --prompt "What is the capital of France?" --max-tokens 16
    target/release/oominf serve --model model.oom

The server listens on `127.0.0.1:8091`. Mac defaults are one active request,
4096 context tokens, fp32 KV state and prompt lookup disabled. For short answers,
set `"chat_template_kwargs":{"enable_thinking":false}` in an OpenAI request.
The tested HTTP request is in [Hardware](docs/HARDWARE.md#apple-silicon).

Metal and host allocations share a budget based on current reclaimable RAM.
`--vram-expert-gib` caps the shared expert cache; it shrinks to fit.
The default RAM reserve is 3 GiB when at least 6 GiB is currently available,
otherwise 1 GiB. `--host-reserve-gib` overrides it. Embedding rows are read on
demand; dense projections and the output head stay resident.

This path prefills one token at a time and supports the text decoder only.
Vision, MTP, prompt lookup, compressed KV and prefix snapshots are unavailable.
CUDA hardware profiles are unused; `doctor` estimates the shared memory budget
and `bench` measures actual inference. MLX is required only for the independent
validation script in [Testing](docs/TESTING.md#apple-silicon).

## Linux setup

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
    cargo test --workspace         # CPU tests and Mac GPU tests; CUDA tests are ignored

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
