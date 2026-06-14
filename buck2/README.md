# buck2 — Buck2 build rules

Reusable [Buck2](https://buck2.build) rules for building/publishing container
images and Helm charts, maintained here and consumed by Buck2 projects (e.g.
`loom`) as an external cell. These are the Buck2 counterparts to the Bazel rules
under `bazel/helm` and `bazel/tools/oci`, so the public API is kept close to
those for familiarity.

## Layout

- `platforms/` — execution platform for standalone homelab buck2 builds.
- `toolchains/` — the `toolchains` cell (system genrule/cxx/python/test toolchains).
- `bin/` — pinned CLI binaries the rules wrap (`apko`, `crane`, `helm`, `yq`).
- `apko/` — Wolfi/apko image rules (`apko_image`, push, lockfile).
- `oci/` — a Buck2 port of rules_oci (`oci_image`, `oci_pull`, `oci_image_index`,
  `oci_push`, `oci_load` + the `OciImageInfo` provider).
- `helm/` — `helm_chart` / `helm_package` / `helm_push` / `helm_images_values` /
  `argocd_app`.

## Why a second build system in a Bazel repo

homelab is a Bazel monorepo, but Buck2 consumers need these rules. Buck2 reads
`BUCK` files and Bazel reads `BUILD`/`BUILD.bazel`, so the two coexist in one
tree. The buck2 prelude submodule (`/prelude`) is excluded from Bazel via
`.bazelignore`.

## Pins (keep aligned)

- buck2 release: **2026-05-18**
- prelude submodule: **2d4a7426826950b8472ac936053ae092c27c2d31**

These match the `loom` repo, which consumes this cell. Bump both together.

## Building / testing

Local execution only (no buck2 RE yet). From the repo root, in a shell with
`buck2` on `PATH`:

```sh
git submodule update --init --recursive   # fetch the prelude
buck2 build //buck2/...
buck2 test  //buck2/...
```

CI runs the same via the **Buck2 rules** action in `buildbuddy.yaml`.

## Internal-reference rule

All loads/targets inside `buck2/**` use only cell-relative `//buck2/…`
and `prelude//…` — never a bespoke cell name. An external cell's own `[cells]`
config is ignored by the consumer, so this keeps the rules resolving identically
whether built standalone here or consumed by `loom`.
