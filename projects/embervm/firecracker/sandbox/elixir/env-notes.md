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
