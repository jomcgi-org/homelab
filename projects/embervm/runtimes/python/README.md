# EmberVM python runtime base (R1 zip lane)

The zero-toolchain "zip lane" runtime base (ADR embervm/002). An adopter brings
only a zip archive containing a `handler(event, context)` callable; this image
bakes Python 3.12, a fixed dependency subset, and a bootstrap shim that, inside
a disposable Firecracker guest, receives the archive over vsock, unpacks it,
imports the handler, and serves the frozen HTTP-over-vsock guest contract. No
Bazel, no apko, no build step for the adopter.

## What the adopter ships

A single zip archive. Two shapes:

1. **Python handler (default).** The archive contains an importable module with
   a `handler(event, context)` callable. The symbol defaults to `app.handle`
   (i.e. `handle` in `app.py` at the archive root) and is configurable via the
   `EMBER_HANDLER` env var (`module.attr` form).

2. **`bootstrap` escape hatch (any language).** If the archive root contains an
   executable file named `bootstrap`, the shim execs it instead of importing a
   python handler. The `bootstrap` process then owns serving the frozen
   contract itself (same port, same routes). This is how a non-python function
   runs on this base.

The shim unpacks the archive into a tmpfs workdir **guest-side only**: the host
(noded) never unpacks it. noded fetches and sha256-verifies the archive into
memory, boots this guest with **no archive drive**, and POSTs the clean bytes to
the build-only `/shim/hydrate` endpoint over the existing vsock HTTP channel; the
shim expands them only inside the throwaway VM. A zip-slip attempt (a member whose
path escapes the unpack dir) aborts the whole hydrate.

The archive is deliberately **not** attached as a block device. That earlier
design truncated the archive tail (Firecracker floors a virtio-blk device to
whole 512-byte sectors) and pinned the base snapshot to a node-local backing
file (the snapshot recorded the archive drive, so a restore needed the `.img` to
exist). Hydrating over vsock deletes both bug classes: the snapshot is memory +
rootfs only, self-contained and portable (shippable to S3/OCI, restorable on any
node).

## Baked dependency subset (registration-time contract)

This is the exact set an adopter may import with no toolchain. It is the
fc-invoke python sandbox guest's dependency set **including Pillow**, so
numeric, plotting, and image handlers work out of the box. numpy is not pinned
directly (Wolfi ships it only under version-suffixed names); scipy and pandas
pull a compatible numpy in transitively.

| Capability            | Package(s)                 |
| --------------------- | -------------------------- |
| Interpreter           | `python-3.12`              |
| Dataframes / numerics | `py3.12-pandas` (+ numpy)  |
| Plotting (headless)   | `py3.12-matplotlib` (Agg)  |
| Scientific            | `py3.12-scipy`             |
| Imaging               | `py3.12-pillow`            |
| Config parsing        | `py3.12-pyyaml`            |
| Date handling         | `py3.12-python-dateutil`   |

A handler that imports anything outside this set fails at import time, which
(see below) keeps the base from ever going ready. The exhaustive, exact list is
`apko.lock.json`; the table above is the human-facing contract.

## The frozen guest contract

The shim serves HTTP/1.1 over an `AF_VSOCK` listener on **port 1027**
(embervm noded's `vsockproto.GuestHTTPPort`; the host dials it via the
Firecracker CONNECT handshake). Routes:

| Method + path       | Behavior                                                         |
| ------------------- | ---------------------------------------------------------------- |
| `GET /shim/healthz` | Always `200` (liveness; noded's Prime probe).                    |
| `POST /shim/hydrate`| Build-only. Body is the raw zip bytes; unpack + import, flip ready on success (`200`), or report the error leaving ready `False`. See below. |
| `GET /shim/ready`   | `200` **only** after a successful hydrate + handler import, else `503`. |
| `POST <invokePath>` | Marshal the request into `event`, call the handler, marshal back.|

`invokePath` defaults to `/invoke` (matching noded's `defaultInvokePath`) and is
set via `EMBER_INVOKE_PATH`. A `POST` to any other path is `404`; a `POST`
before the shim is ready is `503`.

### Hydration (the build handshake)

On boot the shim starts its vsock HTTP server immediately and answers
`/shim/healthz`, but has no handler and reports `/shim/ready` as `503`. The build
sequence is: noded boots the guest (no archive drive), primes the vsock path with
a `/shim/healthz` probe, POSTs the archive bytes to `/shim/hydrate`, then polls
`/shim/ready` until `200`, then snapshots. On a `/shim/hydrate`:

- **success** (`200`): the shim unpacked the archive to tmpfs and imported the
  handler; `/shim/ready` now returns `200`.
- **empty body** (`400`), **unprocessable archive** (`422`, a bad zip, a zip-slip
  member, or an import error): `/shim/ready` stays `503` and the traceback is
  written to the guest console. noded fails the build with no snapshot.
- **already hydrated** (`409`): a second hydrate once the shim is ready is a
  conflict, not a silent re-import. The build path hydrates exactly once.

### Readiness gates the base build

`GET /shim/ready` returns `200` **only** once a `/shim/hydrate` unpacked the
archive and the handler imported cleanly. If the archive is a bad zip, the module
fails to import, the symbol is absent, or it is not callable, the hydrate returns
a `4xx`/`5xx` and ready never flips. noded's `BuildBase` health-gates on
`/shim/ready`, so a broken function never produces a ready base; the shim writes
the full traceback to stdout/stderr (shipped in the guest logs) and returns it in
the hydrate response body, and the failure surfaces in the Workload condition
rather than hanging silently.

### Event and response shape

Normative, Lambda-compatible-enough. The request the shim builds:

```json
{
  "httpMethod": "POST",
  "path": "/invoke",
  "queryStringParameters": {"k": "v"} ,
  "headers": {"Content-Type": "..."},
  "body": "…",
  "isBase64Encoded": false
}
```

`body` is the UTF-8 request body, or `null` when there is no body. A body that
is not valid UTF-8 (a binary trigger: image, protobuf) is base64-encoded with
`isBase64Encoded: true`, so binary survives the round-trip. A trigger payload
arrives as the body with its content type in `headers`.

The handler returns:

```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "..."},
  "body": "…",
  "isBase64Encoded": false
}
```

`statusCode` defaults to `200`. A base64-flagged `body` is decoded back to raw
bytes before it goes on the wire. For ergonomics, a handler may also return a
bare `str` or `bytes` (served as a `200` body) or any JSON-serializable value
(served as a `200` `application/json` body). A handler that raises returns `502`
with the traceback in the body and on stderr.

## Restore-safe contract (read this)

This image is baked into a **warm base** that is snapshotted once and restored
per invoke. The shim itself caches no wall-clock and seeds no RNG at boot, and
**your handler must not either**:

- Do not read `time.time()` / `datetime.now()` once at import and reuse it. Every
  restored invoke would see the same frozen boot-time value. Read the clock
  inside `handler()`.
- Do not draw entropy (`os.urandom`, `random.seed()` from boot state, a cached
  UUID, a token) once at import and reuse it across invokes. Every restored VM
  would replay the same value. Draw randomness per invocation, inside
  `handler()`.

Anything a function must have unique per-invoke must be produced inside the
handler call, not at module import / boot time.

## Configuration (env vars)

All read at boot; the defaults are baked into the image and can be overridden
per registration.

| Env var             | Default      | Meaning                                             |
| ------------------- | ------------ | --------------------------------------------------- |
| `EMBER_HANDLER`     | `app.handle` | `module.attr` of the python handler callable.       |
| `EMBER_INVOKE_PATH` | `/invoke`    | HTTP path the shim serves the handler on.           |
| `EMBER_HTTP_PORT`   | `1027`       | vsock port (the frozen contract port; do not move). |

## Non-root

Runs as uid/gid 65532 (`runtime` account), `runAsNonRoot` convention. Dual-arch
(x86_64 + aarch64) via the standard apko pipeline.

## Boot integration (Task 7, DONE)

A raw Firecracker boot ignores OCI image config entirely and boots
`init=<HarnessInit>` (see the noded driver's `bootArgs`), so the apko
`entrypoint` that runs the shim is never honoured on a Firecracker boot. This
image therefore ships a real PID 1: `ember-runtime-guest-init`
(`guest-init/cmd/`, a small Go binary mirroring
`projects/firecracker/sandbox/guest-init/`), layered at
`/usr/local/bin/ember-runtime-guest-init` by `../BUILD`.

On boot it:

1. mounts a tmpfs over `/tmp` (`size=256m,mode=1777`) so the shim's unpack dir
   (`/tmp/ember-app`) is writable on the read-only, snapshot-shared rootfs;
2. sets `PATH` plus the baked frozen-contract env defaults (`EMBER_HANDLER` etc.)
   that a raw boot would otherwise lack, without clobbering any per-registration
   override noded injects (the CR's `handler` arrives as `EMBER_HANDLER`);
3. `exec`s `python3 /usr/local/bin/ember-runtime-shim`, so the shim replaces it
   as PID 1 and serves the frozen contract.

The chart wires this init as the `runtime-python` workload's `harnessInit`
(`workloads.runtimePython.harnessInit` in `chart/values.yaml`), which noded maps
to `init=/usr/local/bin/ember-runtime-guest-init` on cold boot. So `/tmp` is
writable (the tmpfs) and the shim runs as PID 1 on a real Firecracker boot. The
OCI `entrypoint` is retained for any non-Firecracker (docker/crane) path, which
ends at the same shim.
