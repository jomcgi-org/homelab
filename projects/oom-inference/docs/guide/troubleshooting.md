# Troubleshooting

Start with `oominf doctor --model model.oom`: it runs the same checks as `serve`
and prints what `serve` would decide, without loading the model. Messages from
`serve` start with `oominf:`; warnings with `oominf: warning:`.

## Start-up says memory does not fit

When even the smallest working configuration does not fit, start-up stops with
the shortfall itemised, for example:

    oominf: model load failed: host memory: the smallest working expert tiers need
    0.8 GiB + 0.5 GiB for transfer and work buffers + 1.3 GiB for prefix
    checkpoints + 3.0 GiB reserve = 5.6 GiB, but only 5.5 GiB is usable; to fit,
    raise the memory (or container) limit, lower --max-context or --max-streams
    (fewer prefix checkpoints), drop --prefix-store-dir or --kv-placement host, or
    lower --host-reserve-gib

The message ends with what to change. For host memory: give the process more
RAM (or raise the container's limit), lower `--max-context` or `--max-streams`,
drop `--prefix-store-dir` or `--kv-placement host`, or set a smaller
`--host-reserve-gib`. For VRAM (`VRAM: the smallest working expert tiers
need ...`): free GPU memory (other processes on the GPU), use `--dense fp8`, lower
`--max-context`, or use `--kv-placement host`. The tested minimum is an 8 GiB
container with a 24 GB GPU ([hardware.md](hardware.md)).

Before it fails, the engine shrinks and warns. These are not errors:

- `requested X for the expert tiers but only Y fits ...; using Y`: a
  `--host-expert-gib` or `--vram-expert-gib` that does not fit was shrunk.
- `lowered the reserve from X to Y to fit the smallest working expert tiers`:
  the default reserve was lowered so the server can start. Expect lower speed.

"Usable" host memory is the smaller of `MemAvailable` and the container's
remaining limit. If it is much lower than you expect, check what else is using
memory and whether you run inside a container or a systemd scope with
`MemoryMax`.

## The process is killed, or CUDA runs out of memory

- **Killed with no message (OOM killer):** something else grew into the memory
  the engine planned for, or a limit the engine cannot see applies. Raise
  `--host-reserve-gib` to leave more room, or cap `--host-expert-gib`.
- **CUDA out of memory during a long request:** lower `--max-context`, or free
  VRAM for the KV cache with `--dense fp8` or `--kv-placement host`. Other
  programs on the GPU (a desktop, another model) take VRAM the engine counted as
  free at start; cap `--vram-expert-gib` or raise `--vram-reserve-gib`.
- **400 `prompt is N tokens; the context limit is M`:** the prompt is longer
  than `--max-context`. Restart with a larger `--max-context` or send less.

## GPU and CUDA errors

| Message                                                                            | Cause                                                                      | Fix                                                                                         |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `this build has no CUDA kernels: nvcc was not found when oominf-cuda was compiled` | Built without the CUDA toolkit                                             | Install it (or set `NVCC`), then rebuild ([install.md](install.md))                         |
| `the CUDA backend is not available on this platform`                               | A macOS (or other non-Linux) build                                         | Serve on Linux; see [install.md](install.md#macos-development-build-only)                   |
| `DriverError(CUDA_ERROR_INVALID_PTX, "a PTX JIT compilation failed")`              | Kernels built for a newer GPU architecture than yours (`OOMINF_CUDA_ARCH`) | Rebuild with your GPU's architecture ([install.md](install.md#2-pick-the-gpu-architecture)) |
| A panic about loading `libcuda` or `libcublas`                                     | The driver or cuBLAS library is not on the loader path                     | Check `ldconfig -p`; add `/usr/local/cuda/lib64` to `LD_LIBRARY_PATH`                       |

The `CUDA_ERROR_INVALID_PTX` line was reproduced by building for `compute_120`
and starting on an RTX 4090.

## Disk read path fallbacks

Each start tries a real read on each way of reading experts and logs the one in
use:

    oominf: expert reads: O_DIRECT through io_uring

Where something is unavailable it falls back, with a warning naming the reason:

| Log                                                                                                   | Meaning                                                                        | Cost                                               |
| ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------- |
| `expert reads: O_DIRECT pread on a thread pool (io_uring unavailable)`                                | io_uring is blocked (a container's seccomp profile) or missing (an old kernel) | Matched io_uring in testing                        |
| `expert reads: buffered pread on a thread pool, pages dropped after each read (O_DIRECT unavailable)` | The filesystem does not support O_DIRECT                                       | About 20% slower prefill and a third slower decode |
| `read path X does not work here`                                                                      | `--io X` forced a path that does not work                                      | Drop `--io` or pick another                        |
| `no read path works for ...`                                                                          | The model file cannot be read at all                                           | Check the path and permissions                     |

If io_uring is blocked in a container, also note that the n-gram table reads use
io_uring on Linux whatever `--io` says; this combination has not been tested.

## Slow-drive and slow-link warnings

The hardware profile warns when the machine is slow enough to matter:

| Warning                                                                                                         | What it means                                                                                                   |
| --------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `slow drive: X GB/s for scattered record reads; a cold prefill ... about N s, and decode misses wait M ms each` | Scattered reads under 1 GB/s. Cold prompts and decode misses will be slow. Use a local NVMe drive for the model |
| `slow start-up: reading X GB of dense weights takes about N s`                                                  | Loading will take over a minute                                                                                 |
| `slow PCIe: X GB/s host to device; every host-tier hit waits N us for its copy`                                 | Under 6 GB/s: an old or narrow PCIe slot, or a riser. Check the GPU's slot and link width                       |

The profile is cached; after changing hardware, `serve --reprobe` or `oominf
tune` measures again.

## 429 Too Many Requests

The server is running `--max-streams` requests (default 2) and `--max-queued`
more are waiting (default 16). Further requests get 429 with `Retry-After: 5`.
Clients should retry after that delay (the OpenAI and Anthropic SDKs do). If it
happens under normal load, raise `--max-queued` (requests wait longer) or
`--max-streams` (each request decodes slower). `/v1/stats` shows `queued` and
`rejected`.

## 503 "model is loading"

The server answers before the model has loaded. Wait until `/health` returns
`ok` (about half a minute on the reference machine, longer with a slow drive or
`--dense fp8` on few CPU cores).

## Reporting a bug

Include:

1. `oominf doctor --model model.oom` output, with the same flags you pass to
   `serve`:

       oominf doctor --model model.oom [your flags] > doctor.txt 2>&1

   It lists the GPU, driver, CPU, RAM, drive, read path, profile and the
   memory plan.

2. The `serve` command line and its log from start to the problem.
3. `curl -s localhost:8091/v1/stats` while or after it happens.
4. The commit you built (`git rev-parse HEAD`), `nvidia-smi` and `nvcc --version`.
5. The request that triggers it, if it is a request.
