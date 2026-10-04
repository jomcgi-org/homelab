# oom-inference

A minimal, extensible inference engine for Mixture-of-Experts models larger than
GPU plus host memory, tuned for one or two interactive streams on one machine.
Routed experts are tiered across VRAM, pinned host memory and NVMe; weights are
used exactly as released. The CLI is `oominf`.

- [Architecture](docs/ARCHITECTURE.md): crates, interfaces, precision, tiers,
  and how to add a model or a platform.
- [Weight format](docs/FORMAT.md).
- [Testing](docs/TESTING.md): reference fixtures, gates and protocol specs.

## Use

    oominf convert --src <release checkpoint> --out model.oom
    oominf verify model.oom
    oominf serve --model model.oom            # OpenAI and Anthropic APIs on :8091
    oominf generate --model model.oom --prompt "Hello"
    oominf bench --model model.oom            # decode speed and tier statistics

## Build

The workspace builds with Cargo (it is excluded from Bazel via `.bazelignore`).
The CUDA backend compiles `crates/oominf-cuda/kernels/*.cu` with `nvcc` (override
with `NVCC` and `OOMINF_CUDA_ARCH`).

    cargo build --release
    cargo test --workspace
