# rules_wrangler

Bazel rules for deploying static sites to Cloudflare Pages via the `wrangler`
CLI. A front-end project that ships as a Cloudflare Pages site declares a
`wrangler_pages` target and a matching `wrangler_pages_push` target for
deployment.

Nothing here touches a Kubernetes cluster; the sites are pure static assets
sent directly to Cloudflare's edge.

> Note: the repo currently has no active Cloudflare Pages sites. The public web
> (apex `jomcgi.dev`, `/docs`, the `/app/*` apps) moved to the monolith's
> read-only public tier (ADR docs/002), so this ruleset has no consumers today.
> It is retained for reuse; see the follow-up on whether to remove it.

## Public API

Everything is loaded from the single facade `//bazel/wrangler:defs.bzl`. Do
not load `pages.bzl` directly.

```python
load("//bazel/wrangler:defs.bzl", "wrangler_pages")
```

| Symbol           | Kind  | Source      | Use                                                      |
| ---------------- | ----- | ----------- | -------------------------------------------------------- |
| `wrangler_pages` | macro | `pages.bzl` | Create a `.push` target that deploys to Cloudflare Pages |

## `wrangler_pages`

High-level macro that emits one target: `<name>.push`. Running that target
invokes `wrangler pages deploy` to upload the built assets.

The wrangler binary is not bundled; you must declare it in the same BUILD file
before calling the macro:

```python
load("@npm//projects/trips/frontend:wrangler/package_json.bzl", wrangler_bin = "bin")
load("//bazel/wrangler:defs.bzl", "wrangler_pages")

# Declare the wrangler binary (one per BUILD file that uses wrangler_pages).
wrangler_bin.wrangler_binary(name = "wrangler")

wrangler_pages(
    name = "my_site",
    dist = ":build_dist",
    project_name = "my-site",
    wrangler = ":wrangler",
    visibility = ["//visibility:private"],
)
```

Deploy with:

```bash
bazel run //projects/trips/frontend:trips.push
```

### Attributes

| Attribute      | Type   | Required | Default | Description                                                                             |
| -------------- | ------ | -------- | ------- | --------------------------------------------------------------------------------------- |
| `name`         | string | yes      |         | Base name; a `<name>.push` executable target is created.                                |
| `dist`         | label  | yes      |         | Label for the built dist directory or a filegroup of assets to deploy.                  |
| `project_name` | string | yes      |         | Cloudflare Pages project name, as it appears in the Cloudflare dashboard.               |
| `wrangler`     | label  | yes      |         | Wrangler binary target. Created with `wrangler_bin.wrangler_binary(name = "wrangler")`. |
| `branch`       | string | no       | `""`    | Git branch for deployment preview URLs. Empty lets wrangler auto-detect from git.       |
| `visibility`   | list   | no       | `None`  | Bazel visibility for the generated targets.                                             |

The `dist` attribute accepts either a single directory output (e.g., the
`build_dist` out-directory from `vite_build`) or a filegroup. When a filegroup
is given, the rule infers the dist root from the common parent of the first
file.

### Static-asset sites (no build step)

If there is nothing to build, point `dist` at a `filegroup` of the raw assets:

```python
filegroup(
    name = "public",
    srcs = glob(["public/**/*"]),
)

wrangler_pages(
    name = "hikes",
    dist = ":public",
    project_name = "jomcgi-hikes",
    wrangler = ":wrangler",
)
```

## How the push works

`wrangler_pages` expands a shell script from `pages_push.sh.tpl` at build time.
The script:

1. Resolves the wrangler binary and dist directory through Bazel runfiles.
2. Checks that `CLOUDFLARE_API_TOKEN` is set; exits with a clear error if not.
3. Runs:
   ```
   wrangler pages deploy <dist> --project-name=<project> --commit-dirty=true [--branch=<branch>]
   ```
4. Forwards any extra positional arguments after `bazel run ... -- <args>` to
   wrangler. Pass `--dry-run` this way for a local sanity check.

The required token needs the **Cloudflare Pages:Edit** permission. Obtain one at
`https://dash.cloudflare.com/profile/api-tokens`.

In CI, `CLOUDFLARE_API_TOKEN` is injected by BuildBuddy; local runs require the
variable in your shell environment.

## Gazelle extension (`gazelle/`)

A Go Gazelle language (`wrangler`) that auto-generates `wrangler_pages` targets
from `wrangler.jsonc` config files. When you drop a `wrangler.jsonc` in a
directory and run `format` (which invokes Gazelle), the extension creates the
corresponding BUILD target automatically.

### How generation works

The extension reads the `name` field from `wrangler.jsonc` and uses it as
`project_name`. The Bazel target name is derived from the directory basename:
the part before the first `.` (e.g., `jomcgi.dev` produces target `jomcgi`,
`docs.jomcgi.dev` produces `docs`).

Generated rules get these defaults:

| Attribute    | Generated value            |
| ------------ | -------------------------- |
| `dist`       | `:build_dist`              |
| `wrangler`   | `:wrangler`                |
| `visibility` | `["//visibility:private"]` |

### Directives

Place these in the BUILD file to control generation:

| Directive                        | Effect                                                                   |
| -------------------------------- | ------------------------------------------------------------------------ |
| `# gazelle:wrangler disabled`    | Skip generation in this directory (use for hand-maintained BUILD files). |
| `# gazelle:wrangler enabled`     | Re-enable generation (overrides a parent `disabled`).                    |
| `# gazelle:wrangler_enabled`     | Alternative form to enable generation.                                   |
| `# gazelle:wrangler_dist :label` | Override the default `dist` label (e.g., `:public` for static sites).    |

Example for a static site whose assets live in `public/` rather than a build
output:

```python
# gazelle:wrangler_dist :public
```

Gazelle preserves hand-authored attributes it does not manage (such as
`branch`), so you can add them after the initial generation without Gazelle
overwriting them.

## Conventions

- Load only from `//bazel/wrangler:defs.bzl`.
- Declare `wrangler_bin.wrangler_binary(name = "wrangler")` in the same BUILD
  file as `wrangler_pages`. Each site package vendors its own wrangler via pnpm
  (loaded from `@npm//path/to/package:wrangler/package_json.bzl`).
- Never run `wrangler` directly from the command line in this repo; always go
  through `bazel run` so Bazel resolves the correct vendored binary and runfiles.
