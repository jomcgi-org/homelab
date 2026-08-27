# EmberVM `iggy` runtime

[Apache Iggy](https://iggy.apache.org) hosted as an EmberVM **stateful-class**
microVM: one long-lived VM owns one writable volume, banks when idle, and wakes
on the next inbound TCP connection. The decision and its rationale are in
[ADR embervm/039](../../../../docs/decisions/embervm/039-iggy-stateful-message-streaming-runtime.md);
this file is the operational shape.

The runtime is modelled directly on `../postgres`, which is the precedent for a
full non-shim guest image with a tap-NIC TCP health gate and an `mmds_env`
secret seam. Read that one first if this one is unfamiliar.

## Layout

| Path | What |
|------|------|
| `apko.yaml` | The Wolfi rootfs: `busybox`, `ca-certificates-bundle`, `e2fsprogs`, `blkid`. 15 packages resolved, no libc dependency of Iggy's own. |
| `guest-init/cmd/` | `ember-iggy-init`, the PID 1 the kernel boots via `init=<path>`. |
| `BUILD` | Layers the vendored `iggy-server` plus `ember-iggy-init` onto the apko base. |

## Where the server binary comes from

`iggy-server` is **not** a Wolfi package and **not** a vendored release tarball.
Upstream routes the `server-*` release tag to Docker Hub
(`.github/config/publish.yml`, `registry: dockerhub`), and the
`iggy-<target>-<version>.tar.gz` bundles its CI builds are
`actions/upload-artifact` artifacts with 30-day retention, not release assets
with a stable URL. So the image **is** the release channel.

`//bazel/tools/oci:oci_binaries` lifts the binary out of the digest-pinned
`apache/iggy` image declared in `MODULE.bazel`. Upstream's image is
`debian:trixie-slim` with `libhwloc15` and `libudev1` installed, but the binary
it carries is built for `x86_64-unknown-linux-musl` and is fully static: no
`PT_INTERP`, no `DT_NEEDED`. None of that Debian base comes across, which is why
this rootfs can stay thin. The rule **asserts** that static property on every
fetch, so an upstream that switches to a dynamically linked build fails the
fetch with a named error rather than producing an image whose binary dies at
exec time on a guest boot.

To move versions: update the `oci.pull` `tag` and `digest` in `MODULE.bazel`
together. Resolve the digest with
`crane digest apache/iggy:<version> --platform linux/amd64`.

## Configuration

Iggy reads **any** config value from an `IGGY_`-prefixed environment variable
(underscores separate nested keys) and falls back to the config compiled into
the binary when it finds no `config.toml`. This runtime therefore ships no TOML
file at all. The defaults live in two places that must agree:

- `apko.yaml`'s `environment` block, for a non-Firecracker (`docker`/`crane`) run.
- `guest-init/cmd/cmdline.go`'s `setDefaultEnv`, which is the **load-bearing**
  copy: a raw Firecracker boot hands PID 1 no environment and never consumes the
  image config.

| Variable | Value | Why |
|----------|-------|-----|
| `IGGY_TCP_ADDRESS` | `0.0.0.0:8090` | The compiled default is `127.0.0.1`, which would leave the workload's TCP health probe over the tap NIC failing forever while the server looked healthy from inside the guest. |
| `IGGY_HTTP_ENABLED` | `false` | The stateful class exposes exactly one port. |
| `IGGY_QUIC_ENABLED` | `false` | On by default upstream; nothing should listen that the Envoy listener cannot route to. |
| `IGGY_SYSTEM_LOGGING_FILE_ENABLED` | `false` | Rotating log files default to on, under `system.path`, capped at 4 GiB with 7-day retention. They would compete with message segments for volume space. `stdout` still carries every line and noded captures it. |
| `IGGY_SYSTEM_PATH` | `<volumeMountPath>/iggy` | Set at launch, not baked: the mount path is only knowable from the kernel command line. |
| `IGGY_ROOT_USERNAME` | `iggy` | Upstream's own default, set explicitly so the credential is fully determined by (this, the Secret) rather than half of it living in a default that could move on a version bump. |
| `IGGY_ROOT_PASSWORD` | **no default, ever** | See below. |

Any of these can be overridden per deployment through the workload's
`secretRef` -> `mmds_env` seam: `setMmdsEnv` runs after `setDefaultEnv`, and
`setDefaultEnv` never overwrites a value that is already set.

## The root password is mandatory on first boot

`ember-iggy-init` **refuses** a first boot with no `IGGY_ROOT_PASSWORD`.

This is not defensiveness about a missing config. Iggy does not fail without one
and does not fall back to a fixed default: it generates a random password and
prints it to stdout exactly once (verified against 0.8.0). The stateful class
boots a volume from scratch exactly once, so the only copy of that credential
would be a single log line, on a datastore that then outlives it by up to
`bankedTtlSeconds`. Refusing the boot turns a silently unusable broker into a
wiring error with a name.

"First boot" is keyed on `<system.path>/state/log` being non-empty, because the
server writes the root user as its first state entry. The obvious alternative,
the presence of `state/info`, is wrong: the server writes the system info
*before* it creates the root user, so a boot interrupted in that window would
leave `info` present with no root user, and an `info`-keyed probe would then skip
the password requirement forever on a volume that still needs to bootstrap one.
The postgres runtime's `PG_VERSION` probe closes the same window.

## Boot classes

One rootfs serves two, exactly as the postgres runtime does.

- **Base build** (no `ember.volume_dev` boot-arg): no volume, no server. Answer
  the vsock `/shim/ready` contract so noded snapshots the warm OS, then hold PID
  1. Exiting PID 1 panics the guest kernel before noded can snapshot.
- **Stateful cold boot** (`ember.volume_dev` / `ember.env.*` present): mkfs the
  volume if blank, mount it, decode the `mmds_env` secrets, launch `iggy-server`
  as a child under uid 65532. Health is a TCP connect to 8090 over the tap, not
  the vsock path.

A **relight** never re-runs kernel init: it resumes the running snapshot, so this
init and its launch happen exactly once per volume generation.

## Sizing

`vcpus: 1`, `memMib: 512`.

Iggy derives its shard count from the CPU topology it sees, so `vcpus` **is** the
shard count and raising it multiplies the per-shard buffers `memMib` has to
cover. Move the two together. An idle server measures roughly 47 MiB RSS; the
512 MiB figure sizes the warm peak, which the fc-base sizing coupling requires
(D-R3.11.1).

Volume: `volumeSizeGiB: 10`. Iggy's default segment size is 1.07 GB and segments
are preallocated per partition, so that is sized for a handful of topics rather
than a bulk log. `volumeSizeGiB` is immutable in v1, so raise it **before**
creating wide topics.
