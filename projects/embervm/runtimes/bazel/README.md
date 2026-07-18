# EmberVM bazel-query demo runtime (ADR embervm/010)

A single apko image, `bazel-query`, that snapshots a WARM Bazel server (the
Abseil analysis graph resident in the JVM heap) as an EmberVM task-class base, so
each visitor to the `/ember/bazel` demo gets a disposable CoW clone that answers
one `bazel cquery` from the restored Skyframe graph in a fraction of a cold
analysis (ADR embervm/010). It is a read-only consumer of the shipped task-class
machinery: `BuildBase` warms + snapshots, `Prime` restores a clone, `Assign`
delivers one query and destroys the VM.

## What is baked (zero egress)

The warming cquery pulls NOTHING off the network. Two Bazel repo rules (wired in
`MODULE.bazel`, ADR embervm/010) bake, per arch:

- **`@bazel_demo_bin`** (a `multiarch_http_file`): the Bazel 7.4.1 binary at
  `/usr/local/bin/bazel`.
- **`@bazel_demo_workspace`** (`bazel/tools/http/bazel_demo_workspace.bzl`): the
  Abseil 20240116.2 checkout at `/opt/abseil` (a WORKSPACE-mode repo root) and a
  distdir of its six checksummed dep archives at `/opt/distdir`. The warming run
  uses `--noenable_bzlmod` + `--distdir=/opt/distdir`, which is strictly simpler
  and fully offline (bzlmod would need vendored BCR registry metadata or a
  network).

The apko base adds a cc toolchain (`gcc` + `glibc-dev` + `binutils`) because
Bazel's `local_config_cc` probe runs a real compiler during the LOADING phase of
the warming run; without one, analysis of Abseil's `cc_*` targets fails toolchain
resolution and the warming run (correctly) fails the base build.

## PID 1: `ember-bazel-init` (`guest-init/cmd`)

A raw Firecracker boot ignores the OCI entrypoint and boots `init=<harnessInit>`,
so this init is the missing PID 1. Unlike the other runtimes there is only ONE
boot class (a plain cold boot, no volume, no mmds_env): the whole point is the
warm snapshot, so the boot warms inline.

1. Mount `/proc` + a large tmpfs at `/tmp`, set `HOME=/tmp/home`.
2. Run the warming `bazel cquery //absl/...` with `Dir=/opt/abseil`.
3. After the warming CLIENT exits cleanly, wait a settle delay, then flip
   `/shim/ready` to 200.
4. Serve the vsock guest contract forever: `GET /shim/healthz`,
   `GET /shim/ready`, `POST /query`.

### The two silent-failure conditions, and how this design eliminates them

The ADR names two ways a warm-snapshot demo can silently degrade into a cold one
(the demo still "works", just slowly, with no error). Both are closed by
construction here:

- **Condition 1: snapshotting a cold server.** The base snapshot must capture the
  server AFTER warming, not during. `noded`'s `BuildBase` cuts the snapshot the
  moment `GET /shim/ready` returns 200, so readiness IS the snapshot trigger.
  This init therefore keeps `/shim/ready` at 503 until the warming client has
  exited 0 and a settle delay has passed. A warming FAILURE never flips ready, so
  the base build fails loudly on `BootReadyTimeout` rather than snapshotting a
  cold or broken server.
- **Condition 2: flag drift between warming and serving.** If a serving query
  used even one flag the warming run did not, Bazel silently discards the
  analysis cache and re-analyzes from cold. So `buildArgv` is the SINGLE Go
  function that builds the argv for BOTH the warming run and every serving query;
  there is no `.bazelrc` and no control-plane-supplied flag. A golden test
  freezes the argv so any edit is loud in review, and the expression is passed as
  exactly one argv element so it can never smuggle a flag.

The proof of both is the `analyzed_line` in the query response: a warm restore
reports `Analyzed N targets (0 packages loaded, 0 targets configured)`. Nonzero
packages loaded means the snapshot was cold or flags drifted; the guest logs a
drift warning when it sees that.

## tmpfs is RAM is the memfile

The base-snapshot rootfs is read-only and shared by every restored clone (`noded`
`RootfsReadOnly`: one rootfs file backs them all, mutable state lives in RAM,
captured by the memfile). So ALL of Bazel's mutable state MUST be on tmpfs:
`--output_user_root=/tmp/bazel` (install base + output base, including the
extracted `external/` tree) and `HOME=/tmp/home` (Bazel's lock + cache). Anything
Bazel tried to write on the rootfs would fail (read-only) or, worse, not survive
into the snapshot. Convenience symlinks are suppressed
(`--experimental_convenience_symlinks=ignore`) because the workspace at
`/opt/abseil` is on the read-only rootfs.

## Sizing

The tmpfs at `/tmp` is `size=1536m`: the extracted `external/` tree (Abseil + six
deps) plus the install base plus the JVM's scratch have to fit in RAM. Too small
an fs makes warming fail with `ENOSPC` (which fails the base build loudly, the
correct behaviour, but is avoidable with headroom). The workload's `memMib`
budgets this tmpfs plus the `-Xmx1g` JVM heap plus the OS; see the
`bazelQueryWorkload` block in the embervm chart values for the RAM arithmetic and
the `cap` governor rationale.
