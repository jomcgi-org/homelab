# Install

| Platform                       | Status                 | What works                                                               |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------ |
| Linux x86-64 + NVIDIA GPU      | Supported              | Everything. Developed and measured on an RTX 4090 (24 GB) with CUDA 13.0 |
| macOS (Apple silicon or Intel) | Development build only | Builds and runs the CPU tests. Cannot serve the model: there is no CUDA  |
| Windows                        | Untested               | Does not build natively today; WSL2 is the likely path but untested      |

## Linux (supported)

You need an x86-64 Linux machine with an NVIDIA GPU, its driver, the CUDA
toolkit and Rust. The model itself needs about 135 GB of local NVMe for the
converted checkpoint, plus room for the download; see the
[quickstart](quickstart.md) and [hardware sizing](hardware.md).

### 1. NVIDIA driver and CUDA toolkit

Install the NVIDIA driver and the CUDA 13 toolkit from your distribution or
NVIDIA's packages. The engine was tested with driver 610.57.04 and CUDA 13.0.

Check both:

    nvidia-smi                 # lists the GPU and the driver version
    nvcc --version             # "release 13.0" or later

The build looks for `nvcc` at `/usr/local/cuda/bin/nvcc`, then on `PATH`; set
`NVCC=/path/to/nvcc` to use another one. Without `nvcc` the build still succeeds
(with a warning) but the binary cannot use the GPU.

At run time `oominf` loads the driver library (`libcuda.so`) and cuBLAS
(`libcublas.so`) through the dynamic loader. The CUDA installer usually
registers them; check with:

    ldconfig -p | grep -E 'libcuda.so|libcublas.so'

If `libcublas` is missing from that list, add the toolkit's library directory:

    export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH

### 2. Pick the GPU architecture

The CUDA kernels are compiled to PTX for one virtual architecture, chosen at
build time with `OOMINF_CUDA_ARCH` (default `compute_89`). Look up your GPU's
compute capability with `nvidia-smi --query-gpu=name,compute_cap --format=csv`.

| GPU generation                          | Compute capability | `OOMINF_CUDA_ARCH`         | Status                                                                     |
| --------------------------------------- | ------------------ | -------------------------- | -------------------------------------------------------------------------- |
| Turing (RTX 20 series) and older        | 7.5 and lower      |                            | Not supported: the kernels use bf16 tensor-core instructions that need 8.0 |
| Ampere (RTX 30 series, A100)            | 8.6, 8.0           | `compute_86`, `compute_80` | Untested                                                                   |
| Ada Lovelace (RTX 40 series, L4, L40)   | 8.9                | `compute_89` (default)     | Tested on an RTX 4090                                                      |
| Hopper (H100)                           | 9.0                | `compute_90`               | Untested                                                                   |
| Blackwell (RTX 50 series, RTX PRO 6000) | 12.0               | `compute_120`              | Untested                                                                   |

PTX built for an older architecture also runs on newer GPUs: the driver compiles
it when the model loads. Setting the exact architecture avoids relying on that.
VRAM matters as much as the generation: see [hardware.md](hardware.md).

### 3. Rust

Install rustup (see [rustup.rs](https://rustup.rs)):

    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

The project pins its compiler in `rust-toolchain.toml`; rustup downloads that
version the first time you run `cargo` inside the project.

### 4. Build

    git clone https://github.com/jomcgi-org/homelab.git
    cd homelab/projects/oom-inference
    OOMINF_CUDA_ARCH=compute_89 cargo build --release

The binary is `target/release/oominf`. To put it on your `PATH`:

    cargo install --path crates/oominf      # installs to ~/.cargo/bin/oominf

(`cargo install` rebuilds, so set `OOMINF_CUDA_ARCH` for it too.) Or copy
`target/release/oominf` anywhere on your `PATH`.

Optionally run the CPU tests (no GPU needed):

    cargo test --workspace

Every test binary should report `ok`; the GPU tests show as `ignored`.

Next: [quickstart](quickstart.md).

## macOS: development build only

On a Mac the workspace builds and the CPU tests run, which is enough to work on
the converter, the tiers' planning and read logic, the server, the scheduler and
the docs. It **cannot serve the model**: the engine's only GPU backend is CUDA,
and macOS has no CUDA. There is no Metal backend (see
[Adding a platform](../dev/architecture.md#adding-a-platform)).

What differs from Linux:

- `serve`, `generate`, `bench`, `tune`, `doctor`, `check-layer` and
  `check-model` exit with an error saying the CUDA backend is not available on
  this platform.
- Expert reads use buffered `pread`: io_uring and O_DIRECT are Linux-only.
- The host memory check reads `/proc/meminfo`, which macOS does not have.

Steps:

1. Install the Xcode command line tools (the linker):

       xcode-select --install

2. Install rustup, then open a new terminal:

       curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

3. Get the source and build:

       git clone https://github.com/jomcgi-org/homelab.git
       cd homelab/projects/oom-inference
       cargo build --release

   The first `cargo` command downloads the pinned Rust toolchain. Expect one
   build warning, and nothing else:

       warning: oominf-cuda@0.0.0: nvcc not found (nvcc): building without CUDA kernels; the CUDA backend will refuse to start (set NVCC to build them)

4. Run the CPU tests:

       cargo test --workspace

   Expect every test binary to report `test result: ok`, with 96 tests passed
   and 18 ignored in total (the ignored ones need a GPU and a converted model).
   The read-path tests skip io_uring and O_DIRECT and test the buffered path.
   One Linux-only benchmark test (`read_throughput`) is not built on macOS.

These counts come from the same commit on Linux, where 96 pass and 19 are
ignored (the extra one is `read_throughput`); the macOS build was checked by
cross-compiling (`cargo check --workspace --tests --target aarch64-apple-darwin`
and `x86_64-apple-darwin`), so report any difference you see.

## Windows: untested

Not tested, and a native Windows build does not compile today: the weight
reader uses Unix positional file reads, and the expert tiers rely on Linux
interfaces (io_uring, O_DIRECT, `mmap`, `/proc/meminfo` and cgroup limits).
`cargo check --workspace --target x86_64-pc-windows-gnu` stops at the first
crate that reads files (`oominf-format`).

WSL2 with NVIDIA's CUDA support is the most likely way to run it on a Windows
machine, following the Linux steps inside WSL2, but nobody has tried it.
