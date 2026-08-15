# ADR embervm/028 Phase 0 results

Measured 2026-08-15. This document discharges the Phase 0 measurement gate in
[ADR embervm/028](../../../docs/decisions/embervm/028-demand-loaded-rootfs-oci-chunk-store.md)
and records the evidence behind each gate decision. It does not freeze the
production manifest or encryption format.

Tracking issue: [#4182](https://github.com/jomcgi/homelab/issues/4182).
Design PR: [#4902](https://github.com/jomcgi/homelab/pull/4902).

The headline result is that the ADR's planning estimate held, and that a
converter flag nobody had considered matters more than the chunker parameters
do. See [Gate 4](#gate-4-one-package-rebuild) before reading anything else.

## 1. Input image identities

Resolved live from the cluster on 2026-08-15, not taken from the digests
recorded on 2026-08-14. All nine noded brick Deployments (`1gi` through `16gi`,
including the per-node pinned variants) carry the same six `GUEST_IMAGE` values,
so there is one image set to measure rather than one per brick class:

```shell
kubectl get deploy -n embervm -o json |
  jq -r '.items[].spec.template.spec.initContainers[]?
         | select(.env[]?.name=="GUEST_IMAGE")
         | "\(.name) \(.env[]|select(.name=="GUEST_IMAGE").value)"'
```

| Image | Digest | Layers | OCI compressed |
| --- | --- | ---: | ---: |
| semgrep | `sha256:186169b08e0464b5bb67a2d77d08cc7f8f1088de8e854a3e06469266a66e006a` | 5 | 767,165,195 |
| runtime-claude | `sha256:cb1dc8f82b27cdcbca1e5813319c32cabce248ca49652d01c3c72ba81113f836` | 7 | 300,990,378 |
| bazel-query | `sha256:3dc4ab017f811a193b13ebc31b624739317526caa44029f35c95bded230c3b6d` | 4 | 277,955,726 |
| sandbox | `sha256:6942ae1489699ccc5492318c2bfb9f26d004914aa7fdda40fbfa48aea7297411` | 4 | 147,638,803 |
| runtime-python | `sha256:a5a6177651a868391e6fd5f7a85f4ee6d174c5d2256441b7b3f94fd81bb14e5d` | 3 | 143,699,696 |
| scratch-postgres | `sha256:35cb32d8c0a8e669b13c08ace3fd51f76780bf8db7b053174d94a871a65db9a8` | 2 | 124,357,016 |
| **Total** | | **25** | **1,761,806,814** |

The compressed total matches the 1,761,806,814 bytes recorded on 2026-08-14
exactly, so the image set has not moved between the two measurements even though
the digests were re-resolved rather than assumed.

Registry paths are `ghcr.io/jomcgi/homelab/projects/embervm/runtimes/{bazel,claude,python,postgres}`
and `ghcr.io/jomcgi/homelab/projects/firecracker/{sandbox,semgrep}/guest`.

## 2. Environment and tool versions

| Component | Version |
| --- | --- |
| `mkfs.erofs` | erofs-utils 1.8.2, Alpine package `erofs-utils-1.8.2-r0` |
| Build container | `docker.io/library/alpine:3.21` under podman, Linux aarch64 |
| `crane` | vendored via `./bootstrap.sh` |
| Harness | `projects/embervm/rootfs/measure_chunks.py` at this commit |
| Python | 3.14.2 |

EROFS is a little-endian on-disk format, so an aarch64 build host produces the
same bytes an amd64 host would for the same input and flags. That is an
assumption this run did not verify, and it is listed under
[limitations](#7-limitations).

## 3. Commands

Export each image as a flattened rootfs tar. `crane export` resolves whiteouts,
verified by finding zero `.wh.` entries in every output tar:

```shell
crane export --platform linux/amd64 "$ref" "tars/$name.tar"
```

Build the candidate EROFS. Every determinism-relevant flag is explicit:

```shell
mkfs.erofs --tar=f -b 4096 -U 00000000-0000-0000-0000-000000000000 \
           -T 0 --mkfs-time --sort=path \
           "erofs/$name.erofs" "tars/$name.tar"
```

| Flag | Why it is here |
| --- | --- |
| `--tar=f` | build from the tar stream, so host uid, gid, and xattr semantics never enter the image |
| `-b 4096` | pin the block size instead of inheriting the host page size |
| `-U 000...0` | pin the filesystem UUID; the default is random per build and alone defeats byte equality |
| `-T 0` | pin the build timestamp |
| `--mkfs-time` | apply `-T` to the superblock only, preserving the real per-file mtimes apko already makes reproducible |
| `--sort=path` | deterministic data ordering for tar input (also the default) |
| no `-z` | uncompressed. Compression would destroy cross-image chunk identity and make the dedup measurement meaningless |

Measure:

```shell
python3 projects/embervm/rootfs/measure_chunks.py images.json \
  --algorithm gear-v1 --minimum 65536 --average 262144 --maximum 1048576
```

The scope model maps every image to Account `homelab` and one principal per
owning workload (`semgrep`, `agents`, `bazel-query`, `sandbox`, `faas`,
`postgres`), which is the real mount-authorization boundary.

## 4. Gate decisions

| # | Gate | Decision | Evidence |
| --- | --- | --- | --- |
| 1 | Byte-identical EROFS twice, versions and flags pinned | **PASS** | [Gate 1](#gate-1-determinism) |
| 2 | Harness run over the deployed set with `fixed-v1` and `gear-v1` | **PASS** | [Gate 2](#gate-2-chunk-measurement) |
| 3 | Per-image, principal, Account, global stored bytes reported | **PASS** | [Gate 2](#gate-2-chunk-measurement) |
| 4 | Fixed and Gear compared across a one-package rebuild | **PASS with a required converter change** | [Gate 4](#gate-4-one-package-rebuild) |
| 5 | Active manifest inventory per brick, separate from dedup | **PASS** | [Gate 5](#gate-5-active-manifest-inventory) |
| 6 | CDC parameters recommended from evidence | **PASS** | [Gate 6](#gate-6-cdc-parameter-recommendation) |
| 7 | Published platform universe decided | **PASS, recommend against for now** | [Gate 7](#gate-7-published-platform-universe) |
| 8 | ublk host capability probe | **OPEN, inconclusive from read-only access** | [Gate 8](#gate-8-ublk-and-firecracker-probes) |
| 9 | Firecracker digest-symlink snapshot experiment | **OPEN, needs a live VM** | [Gate 8](#gate-8-ublk-and-firecracker-probes) |

The ADR's instruction not to freeze the production format on the 13% to 20%
estimate still stands. Gate 4 is why: the measured saving depends on a converter
flag, so the format decision should follow the converter decision.

### Gate 1: determinism

Every candidate was built twice from the same tar with the flags above and
compared by sha256. All seven images, plus both variants built with
`-Enoinline_data`, were byte-identical across passes.

| Image | EROFS bytes | sha256 (both passes) |
| --- | ---: | --- |
| semgrep | 1,280,397,312 | `f5d17a310f6d13047018c8fcfd731a83ee413b63df4489f7cc5c0770bf6ba35c` |
| runtime-claude | 847,413,248 | `3297a560905116c42d74905154ab55a50c4a384da4e00509e9404aa90affa291` |
| bazel-query | 616,206,336 | `5b963d45521814bf344aec1783349cf82e18694d08c13948fbc616ba9f13c5a8` |
| sandbox | 433,328,128 | `ae791112a7a88d094db9ea5d3a1c7836b9486d57d1133f2f549433ed20690f03` |
| runtime-python | 429,387,776 | `1dfbbcf9caa0d2df89b0064b2f8848cdcf85aa78dcd400ebe769b717be556c94` |
| scratch-postgres | 310,312,960 | `7e90949d1f1b5fae9f0ca3c959bf004df253cbdc34abbed619cd976e0d50d3da` |
| runtime-python-rebuild | 429,391,872 | `5c4c300b0c69588c5e4be24e8db4faa43e3e1c3121bf4f7229f33c00492c26ce` |

Dropping `-U` reintroduces a random UUID per build and breaks byte equality on
its own, so it is the flag most worth keeping in any production converter.

The flattened EROFS set totals 3,917,045,760 bytes (3.648 GiB) against the
4,448,763,904 bytes (4.143 GiB) the same six images currently occupy as sparse
ext4 roots. **Changing the format alone recovers 507.1 MiB, or 11.95%, before
any deduplication.** That is not a dedup result and is not counted as one below.

### Gate 2: chunk measurement

Six deployed images, Gear CDC against the fixed-offset baseline. Logical total
3,917,045,760 bytes in both rows.

| Algorithm | Scope | Chunks | Stored bytes | Saved bytes | Saved | Dedup ratio |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `gear-v1` 512K | per_image | 4,151 | 3,917,045,760 | 0 | 0.000% | 1.000000 |
| `gear-v1` 512K | principal | 3,845 | 3,579,762,775 | 337,282,985 | 8.611% | 1.094219 |
| `gear-v1` 512K | **account** | 3,451 | **3,260,105,750** | **656,940,010** | **16.771%** | 1.201509 |
| `gear-v1` 512K | global | 3,451 | 3,260,105,750 | 656,940,010 | 16.771% | 1.201509 |
| `fixed-v1` 512K | per_image | 7,474 | 3,917,045,760 | 0 | 0.000% | 1.000000 |
| `fixed-v1` 512K | principal | 7,469 | 3,914,424,320 | 2,621,440 | 0.067% | 1.000670 |
| `fixed-v1` 512K | account | 7,463 | 3,911,278,592 | 5,767,168 | 0.147% | 1.001474 |
| `fixed-v1` 512K | global | 7,463 | 3,911,278,592 | 5,767,168 | 0.147% | 1.001474 |

**Fixed-offset chunking recovers essentially nothing: 5.5 MiB out of 3.6 GiB.**
Content-defined chunking is not an optimisation here, it is the whole mechanism.

Per image under `gear-v1`, in input order (`new@account` is order-dependent by
construction, the scope totals are not):

| Image | Logical | Chunks | Unique | Mean chunk | New at Account scope |
| --- | ---: | ---: | ---: | ---: | ---: |
| semgrep | 1,280,397,312 | 1,239 | 962 | 1,033,412 | 967,767,251 |
| runtime-claude | 847,413,248 | 943 | 941 | 898,635 | 821,727,972 |
| bazel-query | 616,206,336 | 695 | 668 | 886,628 | 581,585,777 |
| sandbox | 433,328,128 | 446 | 446 | 971,588 | 396,374,553 |
| runtime-python | 429,387,776 | 440 | 440 | 975,881 | 206,614,122 |
| scratch-postgres | 310,312,960 | 388 | 388 | 799,776 | 286,036,075 |

`runtime-python` adds only 206,614,122 bytes of the 429,387,776 it contains,
because `sandbox` was stored first and the two share 51.9% of their chunks. That
is the sandbox and runtime-python package overlap the ADR predicted, now
measured at the filesystem level rather than inferred from apko locks.

`semgrep` reports 1,239 chunks but only 962 unique, so 277 chunks repeat inside
that single image. Intra-image repetition is real and the store gets it for free.

**Measured Account-scope saving is 626.5 MiB, or 16.771%.** The ADR's planning
estimate was 550 to 850 MiB, or 13% to 20%. The measurement lands inside the
predicted range, so the estimate is confirmed rather than replaced.

Because production has exactly one Account today, `global` and `account` are
numerically identical. That is a property of the current deployment, not a
finding about cross-Account reuse, and it is what Gate 7 turns on.

### Gate 4: one-package rebuild

The prior digests for four of the six images are still on the bricks but have
been garbage collected from GHCR (verified: every superseded manifest returns
not-found), so a real consecutive rebuild pair could not be retrieved. The
comparison below uses a **synthetic** one-package rebuild instead, and that is
its main limitation.

The change models an openblas point upgrade against `runtime-python`: one
contiguous MiB of `usr/lib/libopenblasp-r0.3.34.so` rewritten, the file grown by
1,111 bytes, and the versioned filename moved to `libopenblasp-r0.3.35.so`.
Nothing else in the image differs.

| Algorithm | Second build re-stores | Share of the rebuilt image |
| --- | ---: | ---: |
| `fixed-v1` 512K | 315,097,088 | 73.38% |
| `gear-v1` 512K | 204,461,727 | 47.62% |

Gear beats fixed by 35%, but re-storing 47.6% of an image for a one megabyte
change is a bad result, and it is worth understanding rather than accepting.
Decomposing the change into its three independent effects explains it:

| What changed | Re-stored | Share |
| --- | ---: | ---: |
| content only, same name and same size | 1,532,107 | **0.36%** |
| content plus 1,111 bytes of growth | 204,461,727 | **47.60%** |
| content plus growth plus rename | 204,461,727 | 47.60% |

The rename costs nothing. **The chunker is not the problem and neither is the
package: file size change is.** `mkfs.erofs` packs each file's tail into the
inode metadata region, so growing one file re-packs the layout of everything
laid out after it. The relocation is not a uniform shift, which is exactly what
content-defined chunking cannot resynchronise against. A direct check confirms
the mechanism: after the rebuild only 33.6% of 64 KiB blocks are unchanged at
the same offset, and the image tail matches under no uniform shift, even though
the file grew by exactly one 4 KiB block.

The chunker itself was verified independently and is sound. Inserting 1,111
bytes into a raw 32 MiB stream leaves 98.2% of bytes in shared chunks, and only
2.3% of cuts land on the maximum size, so cuts are genuinely content-defined.

Rebuilding with `-Enoinline_data`, which block-aligns every file instead of
inlining tails, removes the effect:

| Layout | One-package rebuild re-stores | Image size cost |
| --- | ---: | ---: |
| default (tails inlined) | 47.60% | baseline |
| `-Enoinline_data` | **0.47%** | +2.57% across the six-image set |

That is a hundredfold reduction in what a package bump costs, for 2.57% larger
images. Determinism still holds: `-Enoinline_data` images were also built twice
and were byte-identical.

**This is the single most important Phase 0 result.** A real package upgrade
almost always changes file sizes. Without `-Enoinline_data`, the store would
re-fetch roughly half of every image on every bump, which is the cost ADR 028
exists to remove. The converter must set it, and the decision belongs in the ADR
before the format freezes.

### Gate 5: active manifest inventory

Active-set avoidance is measured separately from chunk dedup, as required, and
it is the larger number.

Every brick bakes all six images. The manifests a brick actually activates are
visible as built base snapshots under
`/var/lib/embervm/scratch/embervm-noded/snapshots/bases/`. All six probed bricks
across all four nodes show the same six workload bases:

```
bazel-query  claude-runtime  demo-postgres  sandbox-session  sandbox  semgrep
```

Those six workloads resolve to **five** distinct guest images, because `sandbox`
and `sandbox-session` share one image and `demo-postgres` uses the
`scratch-postgres` image.

**`runtime-python` is baked on every brick and has never been activated on any
of them.** It is not unreachable code: `control/lib/embervm/application.ex:603`
resolves `source.zip.runtime` through `runtimePython.guestImage`, so the R1 zip
lane would use it. No zip-lane workload has run on these bricks, so its
533,065,728 bytes (508.4 MiB) are currently pure waste per brick.

A second finding came out of the same inventory. Bricks retain superseded rootfs
files indefinitely. `rootfsReclaim` deletes abandoned build intermediates only,
never a published rootfs, so a brick that survives one image generation
rollover carries two copies of every rebuilt image:

| Brick | Rootfs files | Allocated |
| --- | ---: | ---: |
| `brick-2gi-node-1` | 10 | 6.934 GiB |
| `brick-2gi-node-3` | 10 | 6.934 GiB |
| `brick-16gi` (recently recreated) | 6 | 4.143 GiB |

The 4.143 GiB baseline in the ADR is therefore the **freshly provisioned**
figure. The steady-state figure on a brick that has been up across one rebuild
is 6.934 GiB, and four of its ten rootfs files are for image digests that no
longer exist in the registry at all.

Against that steady state:

| Quantity | Bytes | GiB |
| --- | ---: | ---: |
| current on-disk allocation, long-lived brick | 7,445,078,016 | 6.934 |
| current generation only | 4,448,763,904 | 4.143 |
| active set only, current generation minus runtime-python | 3,915,698,176 | 3.647 |

Active-set hydration alone avoids 508.4 MiB (11.98%) against the fresh baseline,
and 3.287 GiB (47.41%) against the steady state a long-lived brick actually
reaches. **Both exceed the 16.771% chunk dedup saving.** They are additive with
it, and neither requires the chunk store to exist.

### Gate 6: CDC parameter recommendation

Gear sweep over the six deployed images, `min = avg / 4` and `max = avg * 4`,
Account scope. The 512K row is the harness default and uses `min = 256K`, so it
is not strictly in that family.

| Average | Minimum | Maximum | Chunks | Stored bytes | Saved | Saved |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 128 KiB | 32 KiB | 512 KiB | 18,034 | 3,091,821,661 | 825,224,099 | 21.068% |
| 256 KiB | 64 KiB | 1 MiB | 9,076 | 3,163,875,389 | 753,170,371 | 19.228% |
| 512 KiB | 256 KiB | 2 MiB | 4,151 | 3,260,105,750 | 656,940,010 | 16.771% |

Savings rise monotonically as chunks shrink, and the returns diminish: 512K to
256K buys 2.46 points for 2.2x the chunks, 256K to 128K buys a further 1.84
points for another 2x.

**Recommendation: minimum 64 KiB, average 256 KiB, maximum 1 MiB.**

At 9,076 chunks for the whole six-image set, a manifest holding a 32 byte digest
plus an offset and length per chunk is well under a megabyte per Account, so
chunk-count pressure is not a real constraint at this catalogue size. 128 KiB
buys 1.84 more points and doubles the per-hydration request count again, which
matters more for a store round trip than the bytes do. 256 KiB is the point
where the curve has flattened but the request count is still modest.

This recommendation is for the measurement parameters. It should be revisited
once the converter sets `-Enoinline_data`, because the block alignment changes
the byte stream the chunker sees.

### Gate 7: published platform universe

**Recommendation: do not build the optional published platform chunk universe
yet.**

Production runs exactly one Account. The measurement therefore cannot
distinguish Account-scope reuse from global reuse: both are 3,260,105,750 bytes,
because every image belongs to `homelab`. There is no measured cross-Account
demand, so there is no evidence that a published universe would save anything
today, and it would add a distribution surface, a trust boundary, and a
revocation problem for a benefit of exactly zero measured bytes.

The important half of the requirement is satisfied structurally rather than by
measurement: the full 16.771% Account-scope saving is produced entirely by
sharing within `homelab`, and none of it depends on a published universe
existing. **Account-private sharing is not blocked by deferring this.**

Revisit when a second Account exists. The measurement to run then is the same
harness with images assigned to two different accounts, comparing the `account`
and `global` scopes, which is exactly what the existing report already emits.

### Gate 8: ublk and Firecracker probes

Both remain open. Neither could be closed without a cluster mutation, and
`kubectl` is read-only in this repo.

**ublk host capability: inconclusive.** Probed from inside noded pods on all
four nodes. `ublk` appears in no node's `/proc/modules`:

| Node | Kernel | Modules | `ublk` present |
| --- | --- | ---: | --- |
| node-1 | 6.8.0-101-generic | 234 | no |
| node-2 | 6.8.0-134-generic | 229 | no |
| node-3 | 6.8.0-107-generic | 233 | no |
| node-4 | 6.8.0-87-generic | 213 | no |

That is **not** proof the host cannot do ublk. `loop` is also absent from
`/proc/modules` on every node while `/dev/loop7` is mounted, which proves the
absence of a name from that list means "not a loaded module" and not "not
available". A builtin or an unloaded but installable module looks identical from
inside a container. What is certain is narrower and still useful: ublk is not
loaded today, and the noded pod receives only `/dev/kvm` from the host devtmpfs
with no `/lib/modules` mount, so exposing `/dev/ublk-control` would need a chart
change regardless of the kernel answer.

Closing this needs one command on a node host, outside Kubernetes:

```shell
modinfo ublk_drv && grep -i ublk /boot/config-$(uname -r)
```

**Firecracker digest-symlink snapshot experiment: not attempted.** It needs a
live VM, which is a mutation. Reading the driver first makes the experiment
cheaper to design later. `fcclient.PatchDrive` already exists and updates
`path_on_host` on a loaded but not-yet-resumed snapshot, which is the exact
window the experiment needs. It is called in exactly one place in production
code, `noded/fcvm/driver/driver.go:773`, and only for the `volume` drive. The
`rootfs` drive is only ever configured by `PutDrive` on the cold-boot path at
`driver.go:935`, and is never patched. So the open question is specifically
whether Firecracker accepts a **rootfs** `path_on_host` patch between
`LoadSnapshot` and `Resume`, and that path has no coverage today.

## 5. Design invariants

Nothing measured here contradicts the ADR's invariants, and two are now
supported by evidence rather than assertion:

- private immutable rootfs chunks deduplicate only within an Account: the
  measured 16.771% is entirely Account-internal, and Gate 7 declines the
  cross-Account universe
- every chunk of an active manifest is hydrated and verified before READY: Gate
  5 shows the active set is 5 of 6 images, so eager hydration is cheaper than
  the current bake-everything behaviour, not more expensive

The mutable-artifact invariants (random per-artifact encryption for memory
snapshots, workspaces, and volumes; no cross-principal dedup) were not exercised
by this work, which measured immutable rootfs content only.

## 6. Raw reports

Committed under [`phase0/`](phase0/), one JSON per run, exactly as the harness
emitted it:

| File | Run |
| --- | --- |
| `full_gear_512k.json` | six images, `gear-v1`, 256K/512K/2M |
| `full_fixed_512k.json` | six images, `fixed-v1`, 512K |
| `sweep_gear_128k.json` | six images, `gear-v1`, 32K/128K/512K |
| `sweep_gear_256k.json` | six images, `gear-v1`, 64K/256K/1M |
| `rebuild_gear_512k.json` | one-package rebuild, `gear-v1` |
| `rebuild_fixed_512k.json` | one-package rebuild, `fixed-v1` |
| `erofs_build.log` | every build, both passes, with versions and flags |

## 7. Limitations

Read these before quoting any number above.

1. **The one-package rebuild is synthetic.** The real prior digests were
   garbage collected from GHCR before this run. The change was constructed to
   model an openblas point upgrade and is a fair test of chunker behaviour under
   in-place edit plus growth, but it is not an observed apko rebuild.
2. **EROFS was built on aarch64.** The format is little-endian defined so the
   output should be host-architecture independent, but this run did not build
   the same input on amd64 and compare.
3. **`global` scope is not informative here.** One Account exists, so it is
   numerically identical to `account` by construction.
4. **Ratios exclude overhead.** Manifests, encryption metadata, per-chunk
   framing, and filesystem allocation are all outside these numbers, as the
   harness itself states. Real stored bytes will be higher.
5. **The Gate 6 parameters were swept on the default layout**, not on
   `-Enoinline_data` images. Block alignment changes the byte stream, so the
   recommendation should be re-run once the converter flag is settled.
6. **`new_bytes` per image is input-order dependent.** Only the scope totals are
   order-independent. The per-image column shows who paid for shared content
   first, not who owns it.
7. **The ublk answer is unknown, not negative.** See Gate 8.

## 8. Recommended next implementation slice

Not the converter, hydrator, and ublk stack together. In order:

1. **Settle the converter layout.** Adopt `-Enoinline_data`, then re-run the
   Gate 2 and Gate 6 measurements on the aligned images and confirm the chunk
   parameters. This is a small change to a tool that does not exist in
   production yet, and it is the one decision the format depends on.
2. **Reclaim superseded rootfs files.** Independent of the chunk store, cheap,
   and worth 2.791 GiB per long-lived brick today. Four of the ten rootfs files
   on a long-lived brick are for digests that no longer exist in the registry.
3. **Decide what `runtime-python` is for.** It costs 508.4 MiB on every brick
   and has never been activated on any of them. Either the zip lane gets a
   consumer or the image stops being baked everywhere. This is a values.yaml
   change, not a chunk-store change.
4. **Close the ublk question** with one `modinfo` on a node host, before any
   design depends on the answer.

Items 2 and 3 together recover more than the chunk store's measured 626.5 MiB,
at a small fraction of the work. That does not argue against ADR 028, whose
value grows with catalogue size and with closely related image variants. It does
argue for sequencing them first.
