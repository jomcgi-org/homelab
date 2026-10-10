# bazel/tools/js

Bazel helpers for building JavaScript and TypeScript frontends in this
repo. The toolchain is built on [rules_js](https://github.com/aspect-build/rules_js)
and pnpm workspaces. These helpers standardize the build surface across projects
so each frontend BUILD file stays short.

No single facade `defs.bzl` exists here: each `.bzl` file is loaded directly by
its load path.

## Public API

| Symbol             | Load path                               | Kind  | Purpose                                                   |
| ------------------ | --------------------------------------- | ----- | --------------------------------------------------------- |
| `node_modules_tar` | `//bazel/tools/js:node_modules_tar.bzl` | macro | Pack `js_library` node_modules into a tar for apko images |
| `exec_filegroup`   | `//bazel/tools/js:exec_filegroup.bzl`   | rule  | Pin a dependency to the exec (host) configuration         |

## `exec_filegroup`

A thin Bazel rule that forces its `src` dependency to build under the exec
(host) configuration, then re-exports its files unchanged.

The main use case is shielding platform-independent build outputs (Vite-produced
HTML/CSS/JS) from platform transitions triggered by multi-arch container image
rules. Without this wrapper, `py3_image`'s platform transitions can cause
`aspect_rules_js` to select wrong-arch native binaries (esbuild, rollup) for
the non-host variant.

```python
# projects/monolith/BUILD
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")

exec_filegroup(
    name = "frontend_dist",
    src = "//projects/monolith/frontend:build",
)
```

The resulting label is then passed as `data` to the Python image target:

```python
py_venv_binary(
    name = "main",
    data = [":frontend_dist"],
    ...
)
```

## `node_modules_tar`

Packs `js_library` node_modules into a `.tar` file suitable for inclusion in
apko images. Handles scoped packages and generates `.bin` symlinks from each
package's `bin` entries.

```python
load("//bazel/tools/js:node_modules_tar.bzl", "node_modules_tar")

node_modules_tar(
    name = "node_modules_tar",
    deps = ["//:claude_code"],
)

apko_image(
    name = "my_image",
    tars = [":node_modules_tar"],
    ...
)
```

| Arg           | Default                       | Purpose                       |
| ------------- | ----------------------------- | ----------------------------- |
| `deps`        | required                      | `js_library` targets to pack  |
| `package_dir` | `/usr/local/lib/node_modules` | Install path inside the image |

## How it fits the toolchain

```
pnpm workspace (pnpm-lock.yaml)
  └── rules_js translates lockfile to Bazel targets
        ├── npm_link_all_packages()  →  :node_modules/* per package
        ├── exec_filegroup()         →  pins dist to exec config for multi-arch images
        └── node_modules_tar()       →  packs deps for apko images
```

Each frontend lives in its own pnpm workspace package (a `package.json` at
`projects/<svc>/frontend/` or similar). The `@npm//projects/<svc>/...` load
paths are workspace-scoped, so binary targets from different frontends never
collide. The built `dist/` is consumed either by the service's container image
(via `exec_filegroup` + `py3_image`) or deployed directly to Cloudflare Pages
(via `//bazel/wrangler:defs.bzl`).
