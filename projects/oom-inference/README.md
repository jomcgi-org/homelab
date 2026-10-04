# oom-inference

Minimal, extensible MoE inference engine for frontier models on consumer hardware.
The CLI is `oominf`.

Successor to the FreeToken fork (jomcgi-org/freetoken-fork): a pure-Rust serving
process tuned for 1–2 interactive streams, with experts tiered across VRAM, host
memory and disk. No quality-for-speed trades: every change passes the output
parity gate.

## Build

Excluded from Bazel (`.bazelignore`) during initial iteration; build with Cargo:

    cargo build
    cargo test
