# Engine image for the gVisor fork conformance Job

A public image of the Semgrep OSS engine that listens only after the fork. The
conformance Job (`../conformance/`) forks it. The image holds no Pro engine, no
token and no proprietary rule. The 40 rules in `rules.json` are secret-shaped
regular expressions, written for the fork tests.

Image: `ghcr.io/jomcgi-org/homelab/substrate-fork/engine-oss:<semgrep version>-<commit>`.
It is linux/amd64 only and runs as uid 65532.

## What runs

```text
seed (template)                          child (one fork, own netns)
forksrv-net --listen 0.0.0.0:8080
  exec semgrep-core + shim.so
  load 40 rules
  open /scan/target.json  ---- shim stops here
  write "1" to /proc/gvisor/checkpoint
  read "restore\n"  (blocks)   fork -->  read returns
                                         listen on 0.0.0.0:8080
                                         GET /healthz   200 "ok", keep waiting
                                         POST <path>    read the Content-Length body
                                                        (16 MiB at most), 200 OK
                                         scan the body, write the engine stdout
                                         to the connection, shut it, exit 0
```

| Item | Value |
| --- | --- |
| Port | TCP 8080, set by `--listen` |
| Wakeup probe | `GET /healthz`: 200 and `ok`, any number of times |
| Request | one `POST` with `Content-Length` (a number, 16 MiB at most); chunked bodies get 411, a bad length 400 |
| Response | `200 OK`, then the engine stdout: a `.` line, then the engine JSON (`results`, `errors`) |
| Other requests | 400, 405, 411 or 413; the server keeps waiting |
| Slow client | dropped after 10 s in total for the request head and 30 s in total for the body |
| Exit | after the one scan: the engine's exit code, 0 on success |
| Env | none in the template; `forksrv-net` sets the shim's env |

A client skips the `.` line and reads the JSON from the first `{`. The shim
leaves the engine output as it is.

## Files

| File | Role |
| --- | --- |
| `forksrv-shim.c` | `LD_PRELOAD` shim: checkpoint, server, request body in a memfd |
| `forksrv-net.c` | the entrypoint: fixes the env, execs `semgrep-core` |
| `uname.c` | `uname -s`, which `semgrep-core` runs at start; the base has no coreutils |
| `rules.json`, `targets.json` | the 40 rules and the one target `/scan/target.json` |
| `pins.env` | every pin: wheel, base image, builder image, `crane` |
| `compile.sh`, `make-layer.py`, `build.sh` | build steps |
| `smoke.py`, `edge.py` | contract check and bad-request check from outside |
| `engine_test.py` | tests that need no Docker (`bazel test` target `engine_test`) |

The image is the distroless Debian 12 base plus one layer. The layer holds
`semgrep-core` and its libraries from the PyPI wheel (sha256 checked), the
compiled shim and launcher, the rules and a 1 MiB placeholder at
`/scan/target.json`. The layer is byte-reproducible.
The repository builds its other images with apko and Bazel. Those push on merge
to main. This image must publish only on request, so it has its own workflow.

## Build and test

Workflow `Substrate fork engine image` (`.github/workflows/substrate-fork-engine.yml`),
manual only. With `publish` off it builds the image, runs it four times on a
network with no route out (bodies of 1 KB, 16 KB and 512 KiB with `smoke.py`,
then bad and slow requests with `edge.py`) and discards the image. With `publish` on, on
`main` only, it then pushes the tag `<semgrep version>-<commit>`. It fails if
the tag exists, or if the registry answers with anything but "not found".

Locally, with Docker and `crane`: `./build.sh layer OUT`, `./build.sh image OUT`.
`docker load -i OUT/image.tar` loads the result as `engine-oss:local`. Start it
with `FORKSRV_CHECKPOINT=0` for a plain run outside gVisor, then run
`python3 -I smoke.py HOST 8080 16384`.

## Replace the placeholder digest

1. Run the workflow with `publish` on. Read the reference in the run summary:
   `ghcr.io/jomcgi-org/homelab/substrate-fork/engine-oss:<tag>@sha256:<digest>`.
2. In the GitHub package settings of `substrate-fork/engine-oss`, set the
   visibility to public. The workflow token cannot do this.
3. In `conformance/template.yaml` of PR #8586, replace the `image:` line
   (line 23 at head `e1c6ca29063e`) with that reference. The old line holds
   the repository `substrate-fork/semgrep-engine` and an all-zero digest.
   Change nothing else: `command`, the probe and the limits already match.
4. In `gvisor_fork_test.py` of PR #8586, change
   `test_conformance_refuses_a_placeholder_engine_image_before_any_api_call`
   (line 751). It reads the repository template, which now holds a real digest.
   Give it a placeholder instead:
   `_conformance(tmp_path, re.sub(r"@sha256:[0-9a-f]{64}", "@sha256:" + "0" * 64, _TEMPLATE.read_text()))`.
5. Run `bazel test //projects/platform-factory/substrate/gvisor-fork/...`.

## Tested where

| Check | Where |
| --- | --- |
| Layer reproducible, files, modes, pins | `engine_test.py` (`bazel test`) |
| Workflow guards (manual only, main only, one repository, no overwrite) | `engine_test.py`, only in a plain `pytest` run from a checkout: the Bazel runfiles hold no `.github`, so the test skips there |
| Shim and launcher compile with `-Werror` against Debian 12 glibc | `compile.sh` in the pinned builder image |
| Server contract, edge requests, uid 65532, exit 0 | host chroot of the image rootfs |
| Fork from a seed, own veth, 1, 4 and 16 children, 1 KB, 16 KB, 512 KiB | gVisor fork build on node-1 (Round 5 net test) |
| Image build in Docker, the no-route run, the push | the workflow only |
| `/healthz` from the Substrate worker through a child's netstack | a real worker; the Round 5 test replaced the probe |
