# rules_vitepress

Bazel rules and macros for building and deploying a VitePress documentation
site from Markdown sources scattered across the monorepo.

Content is declared close to its source via `vitepress_filegroup`, then
assembled, link-rewritten, built with VitePress, and deployed to Cloudflare
Pages. Nothing here runs a dev server; the output is a production build pushed
by CI.

## Public API

Two load points:

```python
# Declare a collection of Markdown files for inclusion in the site
load("//bazel/vitepress:defs.bzl", "vitepress_filegroup")

# Build and deploy a VitePress site
load("//bazel/vitepress:site.bzl", "vitepress_site")
```

| Symbol                 | Kind     | File       | Use                                               |
| ---------------------- | -------- | ---------- | ------------------------------------------------- |
| `vitepress_filegroup`  | rule     | `defs.bzl` | Declare Markdown files with a docs-site mount path |
| `VitePressContentInfo` | provider | `defs.bzl` | Carries `repo_path`, `vitepress_path`, `files`    |
| `vitepress_site`       | macro    | `site.bzl` | Assemble, rewrite, build, and deploy the site     |

## `vitepress_filegroup`

Declares a collection of Markdown files and the path they should appear at in
the built site. Any package that contributes documentation creates one of these.

```python
# docs/BUILD
load("//bazel/vitepress:defs.bzl", "vitepress_filegroup")

vitepress_filegroup(
    name = "docs",
    srcs = glob(["*.md"]) + glob(["decisions/**/*.md"]),
    vitepress_path = "docs",
    visibility = ["//projects/websites/docs.jomcgi.dev:__pkg__"],
)
```

| Attr             | Type           | Required | Description                                    |
| ---------------- | -------------- | -------- | ---------------------------------------------- |
| `srcs`           | label list     | yes      | `.md` files to include                         |
| `vitepress_path` | string         | yes      | Output directory in the assembled site         |

The rule propagates a `VitePressContentInfo` provider so the site macro can
map each file's repo-relative source path to its docs-site destination path.

## `vitepress_site`

High-level macro that wires together the full pipeline from content collection
to Cloudflare Pages deployment.

```python
# projects/websites/docs.jomcgi.dev/BUILD
load("//bazel/vitepress:site.bzl", "vitepress_site")

vitepress_site(
    name = "docs",
    content = [
        "//docs:docs",
    ],
    extra_srcs = [
        ".vitepress/adr-sidebar.json",
        "index.md",
    ],
    vitepress_config = ".vitepress/config.js",
    wrangler_project = "docs-jomcgi-dev",
)
```

| Arg                | Default                    | Description                                         |
| ------------------ | -------------------------- | --------------------------------------------------- |
| `name`             | required                   | Base name; suffixed targets are generated from it   |
| `content`          | required                   | `vitepress_filegroup` labels to include             |
| `wrangler_project` | required                   | Cloudflare Pages project name                       |
| `vitepress_config` | `.vitepress/config.js`     | Path to VitePress config file                       |
| `extra_srcs`       | `[]`                       | Additional source files passed to the VitePress build |
| `extra_deps`       | `[]`                       | Additional npm dependencies                         |
| `visibility`       | `//projects/websites:__pkg__` | Visibility for the deploy target                 |

The macro emits these targets:

| Target                | Description                                       |
| --------------------- | ------------------------------------------------- |
| `<name>_assemble`     | Copies all content files into a single directory tree |
| `<name>_path_map`     | JSON file mapping repo paths to VitePress paths   |
| `<name>_rewrite`      | Link-rewritten copy of the assembled content      |
| `build`               | VitePress production build (`dist/`)              |
| `<name>`              | `wrangler pages deploy` runner for CI             |

## Pipeline

```
vitepress_filegroup (one per source package)
        |
        v
  _assemble          <- merges all srcs into a flat dir tree
        |
        v
  _path_map          <- emits JSON: { "repo/path": "vitepress/path", ... }
        |
        v
  _rewrite           <- runs rewriter/rewrite.py
        |
        v
  vite_build         <- runs `vitepress build`
        |
        v
  wrangler_pages     <- deploys to Cloudflare Pages
```

## Link rewriter (`rewriter/`)

Markdown files live next to their source code, so their relative links point to
repo paths that do not exist in the built docs site. The rewriter (`rewrite.py`)
fixes this at build time via a three-step process per link:

1. **Resolve** the relative link target against the containing file's repo
   directory to get a canonical repo-relative path.
2. **Remap** that path to its VitePress path via the JSON path map using
   longest-prefix matching.
3. **Validate** the remapped target exists in the assembled tree.

Links that pass all three steps are rewritten to absolute `/vitepress/path`
form. Links that cannot be remapped (files outside the docs site) are stripped
to plain text with a build-time warning; links to anchors or external URLs pass
through unchanged.

Images (`![...]()`) and external URLs (`http://`, `https://`, `mailto:`) are
never touched.

The rewriter binary is `//bazel/vitepress/rewriter:rewrite` and is wired in
automatically by `vitepress_site`; you do not invoke it directly.

## ADR sidebar

`bazel/images/generate-docs-sidebar.sh` is a standalone script (not a Bazel
rule) that scans `docs/decisions/*/` and writes
`projects/websites/docs.jomcgi.dev/.vitepress/adr-sidebar.json`. Run it
manually after adding ADRs; commit the updated JSON. The `vitepress_site` call
passes that file in as `extra_srcs` so it is available to the VitePress config
at build time.

## Conventions

- Load `vitepress_filegroup` from `//bazel/vitepress:defs.bzl` and
  `vitepress_site` from `//bazel/vitepress:site.bzl`.
- Do not load the private `_assemble`, `_path_map`, or `_rewrite` rules
  directly; they are implementation details of the macro.
- Site builds run remotely in BuildBuddy CI, not locally (see `CLAUDE.md`: no
  local test loop). Push the branch and monitor the PR's CI run.
