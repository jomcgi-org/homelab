# elixir sandbox guest environment

Zero-egress Elixir execution sandbox (ADR agents/057). One-shot: each request
runs in a fresh microVM restore and nothing persists. No network access at all.
Code runs as uid 65532 with a hard wall-clock timeout; stdout, stderr, and files
created in the working directory are returned to the caller. Save files with a
plain relative filename (e.g. chart.png), never an absolute path or /tmp, or
they are not collected.

Your code is written to main.exs and run with `elixir main.exs`, so write a
script, not a Mix project: top-level expressions run directly, and IO.puts is
how you return anything as stdout. Defining modules inline with defmodule works
normally.

Elixir and the OTP standard library are all you get. There is no mix deps.get
and no Hex: the guest has no network, so no Jason, no Ecto, no Nx. Use the
built-in modules (Enum, Stream, Map, String, Integer, Float, Date, DateTime,
:math, :crypto, :queue, and the rest of OTP).

The BEAM is pre-warmed before the snapshot, so start-up is fast. Processes,
Tasks, and Agents all work within the single run, but nothing survives it.
Reading from standard input is disabled, so do not wait on IO.gets.

## Installed packages (from the image lock; exact and exhaustive)

| Package | Version |
| ------- | ------- |
| busybox | 1.37.0-r61 |
| ca-certificates-bundle | 20260413-r1 |
| erl28-elixir-1.19 | 1.19.5-r1 |
| erlang-28 | 28.5.0.5-r0 |
| glibc | 2.43-r13 |
| glibc-locale-posix | 2.43-r13 |
| ld-linux | 2.43-r13 |
| libcrypt1 | 2.43-r13 |
| libcrypto3 | 3.6.3-r4 |
| libgcc | 16.1.0-r4 |
| libstdc++ | 16.1.0-r4 |
| libxcrypt | 4.5.2-r4 |
| ncurses | 6.6.20260815-r0 |
| ncurses-terminfo-base | 6.6.20260815-r0 |
| wolfi-baselayout | 20230201-r29 |
| zlib | 1.3.2-r4 |
