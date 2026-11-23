# Pro engine image for the gVisor fork pool

A PRIVATE image of the Semgrep Pro Secrets engine that listens only after the
fork. It has the same contract as the public OSS image (`../engine/`). The scan
tier of `../../egress-scan/` forks it. Nothing here runs until Joe sets the
environment secret and starts the workflow.

Image: `ghcr.io/jomcgi-org/homelab/substrate-fork/engine-pro:<semgrep version>-<commit12>`.
It is linux/amd64 only, runs as uid 65532 and needs no network.

The real Semgrep token is used only at build time. The image holds a
placeholder token. Joe decided this on 2026-11-23 (see "Decided" in
`../egress-scan/ARCHITECTURE.md`).

## What is in the image

The distroless Debian 12 base of `../engine/pins.env` plus one layer:

| Path | What | Source |
| --- | --- | --- |
| `/semgrep/semgrep-core-proprietary` | the Pro engine, 1.186.0 | download with the token |
| `/semgrep/rules.json` | the Secrets rules of the deployment (1586 on 2026-11-22), sorted by id | download with the token |
| `/semgrep/libs/*` | libraries of the OSS wheel (sha256 checked) | `../engine/pins.env` |
| `/semgrep/shim.so` | the `LD_PRELOAD` server: checkpoint, listen, request body | `../engine/forksrv-shim.c`, not copied |
| `/usr/bin/uname` | the engine runs `uname -s` at start | `../engine/uname.c`, not copied |
| `/usr/local/bin/forksrv-net` | the entrypoint, `--listen HOST:PORT` | `forksrv-net.c` here |
| `/semgrep/targets.json`, `/scan/target.json` | one target, the product `secrets`; a 1 MiB placeholder body | here |
| `/semgrep/.tok` | the PLACEHOLDER token, mode 0444 | `pins.env` |
| `/semgrep/BUILD-INFO.json`, `/semgrep/NOTICE` | sha256 of the inputs; what the image is | `make-layer.py` |

The contract is the one of `../engine/README.md`: `forksrv-net` listens on
`0.0.0.0:8080`, `GET /healthz` answers 200 `ok`, one `POST` is scanned and the
engine stdout is the answer, then the process exits 0. The shim opens
`/proc/gvisor/checkpoint` at the first open of `/scan/target.json`, after the
engine loaded and compiled the rules. The engine runs with
`-secrets -disable-secrets-validation -pro_intra_file -json -j 1`.

Why `forksrv-net.c` is its own file: the engine, the flags and the token
differ. The OSS launcher stays as it is, so its published digest still matches
its source. The shim, `uname`, the pins, `smoke.py` and `edge.py` are not copied.

## How the token is used

```text
GitHub environment semgrep-pro-build, secret SEMGREP_APP_TOKEN (main only)
  |
  v   step "Download the Pro engine and the rules"  (token in this step's env)
fetch-pro.py --> semgrep-core-proprietary, rules.json        in RUNNER_TEMP only
  |
  v   steps without the token: build layer, build image
layer.tar, image.tar   (placeholder in /semgrep/.tok)
  |
  v   step "Test the image with the placeholder token"
docker run, network with no route out: planted, planted 512 KiB, clean, edge
  |
  v   step "Scan the layer and the image for the token"  (token in this step's env)
token-scan.py: the real token anywhere = fail; the placeholder only in /semgrep/.tok
  |
  v   publish on, main only
tag check, push, anonymous pull must be refused (else delete the version, fail)
```

- The token is in two steps only. A test (`engine_pro_test.py`) checks this.
  The steps use `set +x`; the download step also uses `::add-mask::`.
- `fetch-pro.py` sends the token as an `Authorization` header over https and
  drops it on a redirect to another host. It prints sizes and hashes only. It
  writes the token to no file. No step uploads an artifact or uses a cache.
- `build.sh` and `make-layer.py` stop when `SEMGREP_APP_TOKEN` is in their
  environment, so a mistake in the workflow cannot put it into the layer.
- The download calls are the ones of the Semgrep CLI for the pinned version:
  `GET /api/agent/deployments/deepbinary/manylinux-x86?version=1.186.0` and
  `GET /api/agent/deployments/scans/config?is_secrets_scan=true&dry_run=true&full_scan=true&semgrep_version=1.186.0`.
  They were not run with a real token while this was written: the first
  workflow run is the first test of `fetch-pro.py` against Semgrep.

## The placeholder token

| Item | Value |
| --- | --- |
| Where it is set | `PLACEHOLDER_TOKEN` in `pins.env` |
| Where the image holds it | `/semgrep/.tok`, one line, mode 0444 |
| What reads it | `forksrv-net` reads the file and sets `SEMGREP_APP_TOKEN` for the engine. It overwrites any inherited value |
| Where the engine checks | the environment variable `SEMGREP_APP_TOKEN`, at start |
| What the engine does with it | the 2026-11-22 spike: it checks that a value is present (`strace`: no `connect`, no `socket`). Without a value it exits 13 ("You must log in with `semgrep login`"). It does not check again after the fork |

The placeholder is public. Every child can read it. That is the point.

A Semgrep update can add an online check or a format check. Then the engine
does not start with the placeholder. Every rebuild runs the planted-secret
scans below with the placeholder, so the build fails and nothing is published.
Then the choice goes back to Joe: a real token in the image (option (a) in the
design note) or no Pro engine.

## Tests the workflow runs

`smoke-pro.py` runs against four fresh containers on a network with no route
out, read-only root, no capabilities, 1 GiB, 64 processes:

| Run | Body | Pass when |
| --- | --- | --- |
| `planted 16384` | 16 KB of filler and 11 fake secrets | 5 results or more, no errors |
| `planted 524288` | 512 KiB, the same secrets | 5 results or more, no errors |
| `clean 16384` | 16 KB of filler | 0 results, no errors |
| `edge` | bad requests, then a planted scan with header-like text in the body | each bad request gets its error; the scan has 5 results or more |

The secrets are made at run time from a fixed seed (the recipe of the spike,
which got 10 results from 9 rules on all 1586). The source holds no complete
token, so secret scanners do not flag it. The count and the rule ids go to the
job summary. The floor of 5 is a trip wire, not a measurement: after the first
good run, Joe or a later change can pin the real count.

The slow-client checks of `edge.py` (25 s) are not repeated. They test the OSS
shim, and this image runs the same shim.

## Publish and the private package

Workflow `Substrate fork engine image (Pro, private)`
(`.github/workflows/substrate-fork-engine-pro.yml`), manual only.

- `publish` off: build, test, scan, discard. Safe to run any time.
- `publish` on, on `main` only (a run from another branch fails in the first
  step): the tag `<semgrep version>-<commit12>` must be free. The step fails if
  the tag exists or if the registry answers with anything but "not found".
- Then `check-private.sh` asks the registry for the manifest with no
  credentials (an empty Docker config, an empty environment). Only
  `UNAUTHORIZED`, `DENIED`, `NAME_UNKNOWN`, `MANIFEST_UNKNOWN` or HTTP 401, 403,
  404 pass. If the manifest comes back, or the answer is unclear three times,
  the script deletes the version with `gh api` and the job fails.

Deleting a version with `GITHUB_TOKEN` and `packages: write` is not verified.
It works only when the workflow's repository has write access to the package,
which GitHub gives to a package that a workflow created. If the delete fails,
the job says "DELETE IT BY HAND" in an error: delete the version in the
package settings and set the package private. A token with more rights is not
needed for a package this workflow created.

Between the push and the check the image can be public for a few seconds. The
placeholder token means the real token is not exposed. The Pro binary and the
rules are, if the check fails. That is part of the licence open item below.

The image has no `org.opencontainers.image.source` label. The label links a
package to the public repository, and the package could inherit its access.

## What Joe does once

1. **Create the environment.** Repository settings, Environments, new
   `semgrep-pro-build`. Set "Deployment branches" to `main` only, and add
   yourself as a required reviewer. A branch of this public repository must not
   be able to read the secret: someone who can push a branch could change the
   workflow to print it. (Before this workflow is on `main`, a branch run cannot
   read the secret at all. Merge it first: nothing runs on a merge.)
2. **Make a dedicated token.** In Semgrep, create a token only for this build,
   not a personal one. It needs the narrowest scope that accepts the two calls:
   download the Pro engine (the deployment must have the Pro engine) and read
   the scan configuration of the deployment. An agent (CI) token is the
   expected kind. The scope is not verified: the first run shows it (HTTP 401 or
   403 in the download step). Name it `engine-pro-image-build`. Put it in 1Password and copy it
   into the environment secret `SEMGREP_APP_TOKEN`. Do not put it in any file or
   chat.
3. **Run once with `publish` off.** From `main`, Actions, run the workflow.
   Read the job summary: the downloaded sizes, the sha256 of the binary, the
   smoke results. Copy `binary_sha256` into `PRO_BINARY_SHA256` in `pins.env`
   (a change through a PR).
4. **Run with `publish` on.** The job pushes, checks the anonymous pull and
   prints `engine-pro:<tag>@sha256:...` in the summary.
5. **Check the package settings once**, in GitHub, Packages, `homelab/substrate-fork/engine-pro`,
   Package settings:
   - visibility is **Private**;
   - "Inherit access from source repository" is **off** (if it is on, switch it
     off: the repository is public);
   - the package grants the atelet's login read access (below).

   These settings live outside the repository. This change could not verify them.
6. **Check that atelet can read it.** See the next section.

## How atelet pulls the private image

atelet pulls actor images itself and ignores pod `imagePullSecrets`. It runs a
credential plugin (`../credential-provider/`, `../values/atelet.yaml`) that
answers every `ghcr.io` image with the login in the file
`/run/ghcr-pull/.dockerconfigjson`. That file is the secret `ghcr-imagepull-secret`
in the namespace `ate-system`, made by the 1Password Operator from the item
`vaults/k8s-homelab/items/ghcr-read-permissions`
(`../resources/ghcr-pull-secret.yaml`, HOME-539). The factory namespace uses
the same item.

The repository does not say which user the login is or what its scope is. So:
confirm that the item's token has `read:packages` and that its user can read
the package. If the package is private and not shared with that user, the pull
fails with `UNAUTHORIZED`. Give the user read access in the package settings
("Manage access"), or test with a tag:
`crane manifest ghcr.io/jomcgi-org/homelab/substrate-fork/engine-pro:<tag>`
logged in with that token. Nothing here changes the pull secret.

## Rotate the token

The token is not in the image, so a rotation needs no new image:

1. Make the new token in Semgrep.
2. Replace the environment secret `SEMGREP_APP_TOKEN`.
3. Revoke the old token.

Rebuild (a run with `publish` on, on a new commit) only to get a newer engine,
newer rules or a changed launcher. A rebuild needs a new commit: the tag holds
the commit and is never overwritten. After a rebuild, change the `image:` line
of the template in the same change that uses it.

## What is reproducible

The layer is byte-reproducible for the same inputs: sorted names, owner 0:0,
mtime 0, fixed modes (`engine_pro_test.py` checks it with a fake engine). The
inputs are not all fixed. The Pro binary and the rules come from Semgrep when
the job runs, so two runs of the same commit can give two different images.
`PRO_BINARY_SHA256` in `pins.env` fixes the binary after the first good run.
The rules change when the deployment's configuration changes. The rule hash and
the binary hash are image labels (`dev.jomcgi.semgrep.rules-sha256`,
`dev.jomcgi.semgrep.engine-sha256`) and are in `/semgrep/BUILD-INFO.json`.

## Open item for Joe: licence

The image holds Semgrep's proprietary engine and rules. Joe said he handles the
licence terms himself. This change assumes nothing about them. Open questions
that are his:

- Do the terms of the deployment allow an engine copy in a private image, run
  on the cluster, without a live token?
- Does the placeholder approach (the engine runs, the token is not in the
  image) fit the terms?
- Is a pull by anyone with access to the private package a use that the terms
  allow? Who has access is in the package settings.

Until Joe decides, run the workflow with `publish` off only.

## Files

| File | Role |
| --- | --- |
| `fetch-pro.py` | download of the engine and the rules; the only reader of the token besides `token-scan.py` |
| `forksrv-net.c` | the entrypoint: placeholder token, the Pro flags, exec |
| `compile.sh`, `make-layer.py`, `build.sh` | build steps; they refuse a token in the environment |
| `token-scan.py` | looks for the real token in a layer, an image or a directory |
| `check-private.sh` | the anonymous pull check and the delete |
| `smoke-pro.py` | planted, clean and edge checks from outside |
| `targets.json`, `pins.env` | the target (product `secrets`) and the pins of this image |
| `engine_pro_test.py` | tests that need no Docker, no network and no token (`bazel test` target `engine_pro_test`) |

## Tested where

| Check | Where |
| --- | --- |
| Pins, shared sources, launcher contract, launcher with a fake engine | `engine_pro_test.py` |
| Layer: reproducible, files, modes, placeholder only in `/semgrep/.tok`, token refused | `engine_pro_test.py` |
| Token scan: plain, reversed, hex, base64, nested layers, chunk edges, placeholder place | `engine_pro_test.py` |
| Private check: stub `crane` and `gh`: pass, delete and fail, delete fails | `engine_pro_test.py` |
| Download: local fake of the two endpoints; header only, sorted rules, no token in files | `engine_pro_test.py` |
| Workflow guards: manual only, token in two steps, no `${{ }}` in `run`, main-only publish, check present | `engine_pro_test.py`, only in a plain `pytest` run from a checkout: the Bazel runfiles hold no `.github`, so the test skips there |
| The Pro engine starts with the placeholder, finds planted secrets, finds none in a clean body | the workflow only (it needs the Pro engine) |
| The real download, the real push, the anonymous refusal | the workflow only; not run yet |
