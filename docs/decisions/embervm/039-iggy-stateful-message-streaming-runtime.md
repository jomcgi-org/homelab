# ADR 039: Apache Iggy as a Stateful-Class EmberVM Runtime

**Author:** Joe McGinley
**Status:** Draft
**Created:** 2026-08-27
**Relates to:** [ADR embervm/001](001-embervm-beam-firecracker-workload-orchestrator.md)
(the stateful class, D-R4.PR-11.1, the workload shape this reuses),
[ADR embervm/008](008-interruptible-bank-stateful-datastores.md) (the two-phase bank this workload
leaves off until it is vetted), [ADR embervm/018](018-node-local-activator-brick-authoritative-lifecycle.md)
(the node-local wake this workload opts into),
[ADR embervm/035](035-website-snapshotter-task-guest.md) (prior art: the most
recent new guest image, on the task class rather than the stateful one)

---

## Problem

The homelab has no message broker. Every producer/consumer relationship between
services is either a direct HTTP call or a row in the monolith's Postgres polled
on a timer. That is fine for the shapes that exist today and bad for the ones
that keep almost existing: fan-out to several consumers, replay of a stream a
consumer missed while it was down, and backpressure that is not "the caller
retries."

Adding a broker to the cluster the obvious way, as a Deployment with a PVC, buys
a component that is always running, always holding memory, and always a thing to
patch, for a workload that will be idle most of the week. That is exactly the
cost profile the EmberVM stateful class exists to remove: one long-lived microVM
that owns one writable volume, banks its memory to a snapshot when idle, and
relights on the next inbound TCP connection. `scratch-postgres` already proves
the shape for a datastore (D-R4.PR-11.1).

So the question is not "should the homelab have a broker" so much as "can a
broker be a stateful-class EmberVM guest, and what does it cost to make one."

Apache Iggy is the candidate: a persistent message streaming server, one static
binary, a plain TCP binary protocol, and disk-backed segments. Nothing about it
obviously needs a pod.

## Decision

Host Apache Iggy as an EmberVM **stateful-class** workload, `iggy`, in a new
`projects/embervm/runtimes/iggy` guest image, exposed cluster-internally on node
Envoy listener port **5402**.

The shape is `scratch-postgres`'s, deliberately and almost line for line: one
singleton VM, one writable volume mounted at `/data`, health as an opaque L4 TCP
connect, secrets over the `secretRef` -> `mmds_env` boot-args seam, a 2-minute
idle bank, a 1-week version-convergence lifetime, and node-local wake so a
producer arriving during a control-plane roll relights on the brick rather than
black-holing.

Five things are specific to Iggy, and each is the answer to something that was
checked rather than assumed. The checks were run against the 0.8.0 binary
directly, not inferred from upstream's `main` (which is on the 0.9 line and
differs).

### 1. The server binary comes out of an OCI image, not a release tarball

`iggy-server` is not in Wolfi, so it cannot be named in an `apko.yaml` the way
`postgresql-16` is. The usual fallback in this repo is `multiarch_http_archive`
against a GitHub release, as `k3s` and `goose` do. That does not work here:
upstream routes the `server-*` release tag to Docker Hub
(`.github/config/publish.yml`, `registry: dockerhub`), and the
`iggy-<target>-<version>.tar.gz` bundles its CI builds are
`actions/upload-artifact` artifacts with a **30-day retention**, not release
assets with a stable URL. The container image **is** the release channel.

So the binary is lifted out of the digest-pinned `apache/iggy` image by a new
repository rule, `//bazel/tools/oci:oci_binaries`. That rule is the thin sibling
of the existing `//bazel/tools/postgres:oci_postgres`, which already does exactly
this for the PostgreSQL test fixture, so the mechanism is precedented in this
repo rather than invented for this ADR.

The thinness is load-bearing and is **checked**. Upstream's runtime image is
`debian:trixie-slim` with `libhwloc15` and `libudev1` installed, which reads like
a dependency the guest rootfs would have to carry. It is not: the binary is built
for `x86_64-unknown-linux-musl` and is fully static (no `PT_INTERP` program
header, no `DT_NEEDED` entries), so none of that Debian base comes with it. The
rule verifies the static property on every fetch and fails the build with a named
error if an upstream rebuild ever breaks it, rather than producing an image whose
binary dies at exec time on a guest boot, where the error would surface as a wake
timeout.

The resulting rootfs is 15 Wolfi packages: `busybox`, `ca-certificates-bundle`,
and `e2fsprogs` + `blkid` for the volume's format-if-blank path. That closure is
identical to the first 15 entries of the postgres runtime's own lock, which is
the same volume machinery minus Postgres.

### 2. Configuration is environment variables, and there is no config file

Iggy reads any config value from an `IGGY_`-prefixed environment variable, and
falls back to the config compiled into the binary when it finds no `config.toml`.
Verified end to end: a run with no config file and only environment variables
starts, logs "Using default configuration embedded into server", applies each
override by name, and serves.

This runtime therefore ships **no TOML file**. That matters more than it sounds:
a baked config file would be a second source of truth living inside a
digest-pinned image, changeable only by rebuilding and re-basing every guest,
while the `mmds_env` seam already delivers per-deployment values without touching
the image. Keeping configuration in environment variables keeps the image
generic and the deployment-specific parts in the Workload CR, where the rest of
the fleet keeps them.

Three overrides are not optional:

- `IGGY_TCP_ADDRESS=0.0.0.0:8090`. The compiled default is `127.0.0.1`, which
  would leave the workload's TCP health probe over the tap NIC failing forever
  while the server looked perfectly healthy from inside the guest. This is the
  single most likely way to get this runtime wrong.
- `IGGY_HTTP_ENABLED=false` and `IGGY_QUIC_ENABLED=false`. The stateful class
  exposes exactly one port; nothing should listen that the Envoy listener cannot
  route to. (HTTP is already off upstream; QUIC is on.)
- `IGGY_SYSTEM_LOGGING_FILE_ENABLED=false`. Rotating log files default to **on**,
  under `system.path`, capped at 4 GiB total with 7-day retention. On this
  workload `system.path` is the volume, so the default would let logs compete
  with message segments for a 10 GiB budget. `stdout` still carries every line
  and noded captures it.

A WebSocket listener also comes up on `127.0.0.1:8092`. It is guest-local, has no
Envoy listener, and is left alone.

### 3. A first boot with no root password is refused

Iggy does **not** fail when `IGGY_ROOT_PASSWORD` is unset, and does not fall back
to a fixed default. It generates a random password and prints it to stdout
exactly once. Verified: `Using the default root user credentials...` followed by
`Generated root user password: <random>`.

On a task-class guest that would be a nuisance. On the stateful class it is a
trap, because a volume bootstraps exactly once, ever: the only copy of the
credential would be a single line in one boot's log, on a datastore that then
outlives that log by up to `bankedTtlSeconds` (30 days). The failure is silent
and total, and it surfaces long after the boot that caused it.

So `ember-iggy-init` refuses the boot instead, naming the CR's `secretRef` and
the Secret key. A wedged wake with a named cause beats an unusable broker that
looks healthy.

"First boot" is keyed on `<system.path>/state/log` being non-empty, because the
server writes the root user as its **first** state entry. The obvious
alternative, the presence of `<system.path>/state/info`, is wrong: the server
writes the system info *before* it creates the root user, so a boot interrupted
in that window (an aggressive idle-bank, a destroy mid-boot) would leave `info`
present with no root user, and an `info`-keyed probe would skip the password
requirement forever on a volume that still needs to bootstrap one. This is the
same window the postgres runtime's `PG_VERSION` probe documents, and the same
fix.

### 4. There is no bootstrap step to gate, unlike Postgres

`scratch-postgres`'s init carries a real branch: `initdb`-if-empty, then
`postgres`. Iggy has no such split. The server creates its own system info, root
user, and empty stream set in-process on a first boot, and loads them on every
later boot, idempotently. Verified by booting twice against the same directory:
the second boot logs `Loaded system info, version: 0.8.0`, `System version 0.8.0
is up to date`, `Loaded 1 state entries` and serves.

`ember-iggy-init` is correspondingly simpler than its postgres sibling: prepare
the directory, refuse a first boot with no root password, launch, flip ready on a
local TCP connect. No `initdb`, no `pg_hba` reconciliation, no post-start
database creation.

### 5. vCPU count is the shard count

Iggy derives its shard count from the CPU topology it sees and pins one thread
per core. On this workload's `vcpus: 1` that is one shard. The coupling is
recorded in the CR because it is invisible otherwise: raising `vcpus` multiplies
the per-shard buffers that `memMib` has to cover, so the two move together. An
idle server measures roughly 47 MiB RSS; `memMib: 512` sizes the warm peak, as
the fc-base sizing coupling requires (D-R3.11.1).

### Shipped inert

The workload lands `enabled: false` in production, with `guestImage.repository`
emptied so the base builder does not render either. Both lines have to move to
turn it on, and the 1Password item carrying `IGGY_ROOT_PASSWORD` has to exist
first, or the first boot is refused by design (decision 3 above).

This is deliberately more conservative than `scratch-postgres`, which ships
`enabled: false` but keeps its repository set. That workload's base is shared:
`demo-postgres` runs off it. Nothing shares this one, so leaving the repository
set would have every production node mkfs and hold a multi-GB base rootfs for a
workload nothing is using yet.

## Consequences

**The homelab gets a broker whose idle cost is a snapshot on disk.** That is the
whole point of putting it on the stateful class rather than in a Deployment. The
tradeoff is that the first producer after an idle window pays a wake, not a
connect.

**A new vendoring mechanism now exists, and it is general.** `oci_binaries`
applies to any upstream that ships only a container image. That is a real
capability and also a real hazard: it makes it easy to pull binaries out of
third-party images without thinking about what else is in them. The rule's static
check is the guardrail (it refuses anything needing a loader, which is most
things), but the discipline is on the caller.

**Iggy is an incubating Apache project on a fast-moving line.** The pinned 0.8.0
is the current stable release; upstream's `main` is already on 0.9 and its config
surface has moved (the 0.9 line adds `IGGY_SHARD_*` knobs that do not exist
here). Version bumps need the facts in this ADR re-verified against the new
binary, not assumed. The digest pin means a bump is explicit, which is the point.

**Two things this change cannot prove and CI must.** The guest kernel is the kata
bundle's, and Iggy's shard allocator pins threads to CPUs and binds memory to a
NUMA node; those are unprivileged calls that a Firecracker guest should serve,
but "should" is not "does", and the first real cold boot is where that gets
settled. The image build itself (apko resolve, the `oci_binaries` fetch, the
layer assembly) also has not run: no `bazel`, `apko`, or `helm` was available in
the environment this was written in. What *was* verified is listed above and in
the PR.

**The listener range is one port closer to full.** 5400 is `scratch-postgres`,
5401 is `demo-postgres`, 5402 is this. The declared capacity is 5400-5409, so
seven remain.

## Alternatives considered

| Option | Why not |
|--------|---------|
| A Kubernetes Deployment plus a PVC, the ordinary way | Always running, always resident, for a workload idle most of the week. The stateful class exists precisely to make that a snapshot on disk instead, and `scratch-postgres` already proved the shape for a datastore. |
| Redis Streams, NATS, or Kafka instead of Iggy | Kafka is far too heavy for a single 512 MiB microVM. NATS and Redis are both credible and would fit the same class; Iggy wins on being one static musl binary with a plain TCP protocol and disk-backed persistence by default, which is the smallest possible guest image and the least configuration surface. Nothing here forecloses adding another broker on the same rung later. |
| Build Iggy from source with `rules_rust` | The repo has a `rules_rust` dependency but no Rust build lane in use, and Iggy's own build needs `cargo-zigbuild`, a pinned nightly-ish toolchain, and an npm step for the embedded web UI. Vendoring a released artifact is the pattern every other third-party guest binary here follows. |
| Vendor the GitHub release tarball with `multiarch_http_archive`, as k3s does | Those tarballs are `actions/upload-artifact` CI artifacts with 30-day retention, not release assets. The URL would rot within a month, and the failure would be a build break at an arbitrary later date. |
| Keep the upstream `apache/iggy` image as the guest rootfs directly | It is `debian:trixie-slim` with an apt layer, several hundred MB, none of which the static binary needs. Every EmberVM guest is an apko Wolfi rootfs, and the base rootfs size is paid on every node that builds the base. |
| Ship it enabled in production | The 1Password item carrying the root password does not exist yet, and by decision 3 its absence refuses the first boot. Enabling before the item exists is a wedged wake. |
| Let Iggy autogenerate the root password | It prints it once to stdout, on a boot that happens once per volume ever. The credential would be unrecoverable as soon as that log rotated, on a datastore that outlives it by up to 30 days. |
