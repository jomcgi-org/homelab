Zero-egress Rust execution sandbox (ADR agents/057). One-shot: each request runs
in a fresh microVM restore and nothing persists. No network access at all. Code
runs as uid 65532 with a hard wall-clock timeout that covers COMPILATION as well
as execution; stdout, stderr, and files created in the working directory are
returned to the caller. Save files with a plain relative filename (e.g.
chart.png), never an absolute path or /tmp, or they are not collected.

Your code is written to main.rs and compiled with `rustc -O main.rs -o main`,
then the binary is run. Write a normal `fn main()`. A compile error comes back
as a nonzero exit with rustc's own diagnostics on stderr, so you see exactly
what the compiler saw.

There is no cargo and no crates.io. The guest has no network, so only the Rust
standard library is available: no serde, no rand, no regex, no itertools.
Anything you would normally pull from a crate has to be written out by hand.

Because rustc runs with optimisations on, a compute-heavy program is fast once
built, but the build itself is charged against your time budget.
