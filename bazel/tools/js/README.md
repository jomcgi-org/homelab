# bazel/tools/js

Bazel macros and rules for building JavaScript and TypeScript frontends in this
repo. The toolchain is built on [rules_js](https://github.com/aspect-build/rules_js)
and pnpm workspaces. These helpers standardize the build surface across projects
so each frontend BUILD file stays short.

No single facade `defs.bzl` exists here: each `.bzl` file is loaded directly by
its load path.

## Public API

| Symbol             | Load path                               | Kind  | Purpose                                                   |
| ------------------ | --------------------------------------- | ----- | --------------------------------------------------------- |
| `vite_build`       | `//bazel/tools/js:vite_build.bzl`       | macro | Standard Vite or Astro build producing a `dist` filegroup |
| `tailwind_build`   | `//bazel/tools/js:tailwind_build.bzl`   | macro | Process CSS with the Tailwind v4 standalone CLI           |
| `node_modules_tar` | `//bazel/tools/js:node_modules_tar.bzl` | macro | Pack `js_library` node_modules into a tar for apko images |
| `exec_filegroup`   | `//bazel/tools/js:exec_filegroup.bzl`   | rule  | Pin a dependency to the exec (host) configuration         |

## `vite_build`

Standard frontend build for Vite-based projects (React, Vue) and Astro sites.
Creates four targets automatically:

- `:node_modules` (linked npm packages, via `npm_link_all_packages`)
- `:src` (`js_library` of all source files)
- `:{name}` (the `js_run_binary` that runs `vite build` or `astro build`)
- `:{name}_dist` (filegroup exposing the built `dist/` for downstream targets)

The consuming BUILD file must declare the binary target itself from the
package-scoped `package_json.bzl` before calling the macro.

```python
# projects/my_app/frontend/BUILD
load("@npm//projects/my_app/frontend:vite/package_json.bzl", vite_bin = "bin")
load("//bazel/tools/js:vite_build.bzl", "vite_build")

vite_bin.vite_binary(name = "vite")

vite_build(
    name = "build",
    srcs = glob(["src/**/*", "public/**/*"], allow_empty = True) + [
        "index.html",
        "package.json",
    ],
    tool = ":vite",
    deps = [
        "react",
        "react-dom",
        "@vitejs/plugin-react",
        "vite",
    ],
    visibility = ["//visibility:public"],
)
```

For Astro projects, pass `config = "astro.config.mjs"` and use the `astro`
binary instead of `vite`. Cross-package dependencies (shared CSS, design
tokens, etc.) go in `bazel_deps` as Bazel labels rather than `deps`.

| Arg          | Default          | Purpose                                               |
| ------------ | ---------------- | ----------------------------------------------------- |
| `srcs`       | required         | All source files for the build (glob recommended)     |
| `tool`       | required         | Label of the vite/astro binary target                 |
| `config`     | `vite.config.js` | Config file; use `astro.config.mjs` for Astro         |
| `deps`       | `[]`             | npm package names to link (strings, not labels)       |
| `bazel_deps` | `[]`             | Bazel labels for cross-package deps (e.g. shared CSS) |
| `out_dir`    | `dist`           | Output directory name                                 |
| `build_args` | `["build"]`      | Arguments forwarded to the tool                       |

## `tailwind_build`

Processes a CSS file with the Tailwind CSS v4 standalone CLI via
`js_run_binary`. Useful when esbuild cannot handle the `@import "tailwindcss"`
directive natively.

```python
load("@npm//projects/my_app:@tailwindcss/cli/package_json.bzl", tailwind_bin = "bin")
load("//bazel/tools/js:tailwind_build.bzl", "tailwind_build")

tailwind_bin.tailwindcss_binary(name = "tailwindcss")

tailwind_build(
    name = "css",
    src = "src/index.css",
    out = "dist/styles.css",
    tool = ":tailwindcss",
    srcs = glob(["src/**/*.tsx"]),
    deps = [":node_modules/tailwindcss"],
)
```

| Arg         | Default  | Purpose                                     |
| ----------- | -------- | ------------------------------------------- |
| `src`       | required | Input CSS file                              |
| `out`       | required | Output CSS file path                        |
| `tool`      | required | Label of the tailwindcss binary target      |
| `srcs`      | `[]`     | Source files Tailwind scans for class usage |
| `deps`      | `[]`     | npm deps needed for CSS processing          |
| `minify`    | `True`   | Pass `--minify` to the CLI                  |
| `sourcemap` | `False`  | Pass `--map` to the CLI                     |

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
        ├── vite_build()             →  runs vite/astro, emits dist/
        ├── tailwind_build()         →  runs tailwindcss CLI, emits CSS
        ├── exec_filegroup()         →  pins dist to exec config for multi-arch images
        └── node_modules_tar()       →  packs deps for apko images
```

Each frontend lives in its own pnpm workspace package (a `package.json` at
`projects/<svc>/frontend/` or similar). The `@npm//projects/<svc>/...` load
paths are workspace-scoped, so binary targets from different frontends never
collide. The built `dist/` is consumed either by the service's container image
(via `exec_filegroup` + `py3_image`) or deployed directly to Cloudflare Pages
(via `//bazel/wrangler:defs.bzl`).
