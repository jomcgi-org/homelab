load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:exclude python_deps.py

load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index", "oci_push")
load(":multitool_tar.bzl", "multitool_tar")
load(":node_tar.bzl", "node_tar")
load(":python_tar.bzl", "python_tar")

multitool_tar(
    name = "tools_tar",
    tools = [
        "argocd",
        "buildozer",
        "crane",
        "gazelle",
        "gh",
        "gofumpt",
        "helm",
        "kind",
        "op",
        "ruff",
        "shfmt",
    ],
)

# Per-platform Node.js binary tars.
# Creates :node_tar_linux_amd64, :node_tar_linux_arm64, :node_tar_darwin_arm64
# each containing /usr/bin/node for the target platform.
node_tar(name = "node_tar")

# Package pnpm from the aspect_rules_js-managed @pnpm repo.
# pnpm is a JS script (cross-platform) that runs on Node.js.
# Places the package at /usr/local/lib/node_modules/pnpm/
# with a symlink at /usr/bin/pnpm for PATH discovery.
# nosemgrep: single-npm-package-in-apko-genrule -- pnpm is self-contained, not a single JS module
genrule(
    name = "pnpm_tar",
    srcs = ["@pnpm//:pkg"],
    outs = ["pnpm.tar"],
    cmd = """
        set -e
        WORK=$$(mktemp -d)
        DEST="$$WORK/usr/local/lib/node_modules/pnpm"
        mkdir -p "$$DEST"
        SRC_DIR=$$(echo '$(location @pnpm//:pkg)' | tr ' ' '\\n' | head -1)
        cp -rL "$$SRC_DIR/." "$$DEST/"
        chmod 755 "$$DEST/bin/pnpm.cjs"
        mkdir -p "$$WORK/usr/bin"
        ln -s "../local/lib/node_modules/pnpm/bin/pnpm.cjs" "$$WORK/usr/bin/pnpm"
        tar -cf $@ -C "$$WORK" .
        rm -rf "$$WORK"
    """,
)

# Package prettier + prettier-plugin-svelte (+ peer svelte) from the pnpm
# lockfile-managed npm packages. Plugins live next to prettier under
# /usr/local/lib/node_modules/; /usr/bin/prettier is a wrapper that sets
# NODE_PATH so require("prettier-plugin-svelte") from the repo config resolves
# without a full workspace node_modules tree (bootstrap / ci lint path).
# nosemgrep: single-npm-package-in-apko-genrule -- intentional multi-package node_modules for PATH prettier plugins
genrule(
    name = "prettier_tar",
    srcs = [
        "//:node_modules/prettier",
        "//:node_modules/prettier-plugin-svelte",
        "//:node_modules/svelte",
    ],
    outs = ["prettier.tar"],
    cmd = """
        set -e
        WORK=$$(mktemp -d)
        NM="$$WORK/usr/local/lib/node_modules"
        mkdir -p "$$NM" "$$WORK/usr/bin"

        copy_pkg() {
            local name="$$1"
            local locations="$$2"
            local dest="$$NM/$$name"
            mkdir -p "$$dest"
            local src
            src=$$(echo "$$locations" | tr ' ' '\\n' | grep "/$${name}$$" | head -1)
            if [[ -z "$$src" ]]; then
                src=$$(echo "$$locations" | tr ' ' '\\n' | grep "$${name}$$" | head -1)
            fi
            if [[ -z "$$src" || ! -d "$$src" ]]; then
                echo "ERROR: could not locate package dir for $$name in: $$locations" >&2
                exit 1
            fi
            cp -rL "$$src/." "$$dest/"
        }

        copy_pkg prettier '$(locations //:node_modules/prettier)'
        copy_pkg prettier-plugin-svelte '$(locations //:node_modules/prettier-plugin-svelte)'
        copy_pkg svelte '$(locations //:node_modules/svelte)'

        chmod 755 "$$NM/prettier/bin/prettier.cjs"
        cat > "$$WORK/usr/bin/prettier" << 'WRAPPER'
#!/usr/bin/env bash
# Ensure plugins packaged next to prettier resolve from any config require().
export NODE_PATH="/usr/local/lib/node_modules$${NODE_PATH:+:$$NODE_PATH}"
if [[ -x /usr/bin/node ]]; then
  exec /usr/bin/node /usr/local/lib/node_modules/prettier/bin/prettier.cjs "$$@"
fi
exec /usr/local/lib/node_modules/prettier/bin/prettier.cjs "$$@"
WRAPPER
        chmod 755 "$$WORK/usr/bin/prettier"
        tar -cf $@ -C "$$WORK" .
        rm -rf "$$WORK"
    """,
)

# Package the homelab CLI into the tools image.
# Source files go into the python_deps runfiles tree so they're importable
# via the shared Python runtime (tools.cli.main). A wrapper script at
# /usr/bin/homelab execs the module.
genrule(
    name = "homelab_cli_tar",
    srcs = [
        "//tools:__init__.py",
        "//tools/cli:__init__.py",
        "//tools/cli:auth.py",
        "//tools/cli:knowledge_cmd.py",
        "//tools/cli:main.py",
        "//tools/cli:output.py",
        "//tools/cli:scheduler_cmd.py",
        "//tools/cli:tasks_cmd.py",
    ],
    outs = ["homelab_cli.tar"],
    cmd = """
        set -e
        WORK=$$(mktemp -d)
        # Place source files in the python_deps runfiles workspace root
        # so `from tools.cli.main import app` resolves naturally.
        DEST="$$WORK/bazel/tools/image/python_deps.runfiles/_main"
        mkdir -p "$$DEST/tools/cli"
        cp $(location //tools:__init__.py) "$$DEST/tools/__init__.py"
        cp $(location //tools/cli:__init__.py) "$$DEST/tools/cli/__init__.py"
        cp $(location //tools/cli:auth.py) "$$DEST/tools/cli/auth.py"
        cp $(location //tools/cli:knowledge_cmd.py) "$$DEST/tools/cli/knowledge_cmd.py"
        cp $(location //tools/cli:main.py) "$$DEST/tools/cli/main.py"
        cp $(location //tools/cli:output.py) "$$DEST/tools/cli/output.py"
        cp $(location //tools/cli:scheduler_cmd.py) "$$DEST/tools/cli/scheduler_cmd.py"
        cp $(location //tools/cli:tasks_cmd.py) "$$DEST/tools/cli/tasks_cmd.py"
        # Wrapper script — uses the vendored venv Python so pip packages resolve
        mkdir -p "$$WORK/usr/bin"
        cat > "$$WORK/usr/bin/homelab" << 'WRAPPER'
#!/usr/bin/env bash
SCRIPT_DIR="$$(cd "$$(dirname "$$0")/../.." && pwd)"
RUNFILES="$$SCRIPT_DIR/bazel/tools/image/python_deps.runfiles/_main"
PYTHON="$$RUNFILES/bazel/tools/image/.python_deps/bin/python3"
exec env PYTHONPATH="$$RUNFILES" "$$PYTHON" -m tools.cli.main "$$@"
WRAPPER
        chmod 755 "$$WORK/usr/bin/homelab"
        tar -cf $@ -C "$$WORK" .
        rm -rf "$$WORK"
    """,
)

# Shim py_venv_binary that declares all monorepo pip deps.
# The binary itself does nothing — it exists solely so py_image_layer can
# package its transitive dependency tree (interpreter + stdlib + pip packages).
# Platform-conditional packages (e.g. pyobjc on macOS, dbus_fast on Linux)
# are handled by rules_python's platform-aware wheel resolution.
py_venv_binary(
    name = "python_deps",
    main = "python_deps.py",
    deps = [
        # keep
        "@pip//httpx",  # keep
        "@pip//typer",  # keep
    ],
)

# Per-platform Python runtime + pip deps tars.
# Creates :python_tar_linux_amd64, :python_tar_linux_arm64, :python_tar_darwin_arm64
# each containing py_image_layer tars (interpreter + stdlib + pip packages)
# for the target platform.
#
# NOTE: darwin_arm64 is experimental — py_image_layer was designed for Linux
# OCI container images and may not produce valid tars for macOS. If the
# darwin variant fails to build, the Linux variants are still usable.
python_tar(
    name = "python_tar",
    binary = ":python_deps",
)

oci_image(
    name = "image_linux_amd64",
    architecture = "amd64",
    os = "linux",
    tars = [
        ":tools_tar_linux_amd64",
        ":node_tar_linux_amd64",
        ":python_tar_linux_amd64",
        ":pnpm_tar",
        ":prettier_tar",
        ":homelab_cli_tar",
    ],
)

oci_image(
    name = "image_linux_arm64",
    architecture = "arm64",
    os = "linux",
    tars = [
        ":tools_tar_linux_arm64",
        ":node_tar_linux_arm64",
        ":python_tar_linux_arm64",
        ":pnpm_tar",
        ":prettier_tar",
        ":homelab_cli_tar",
    ],
)

oci_image(
    name = "image_darwin_arm64",
    architecture = "arm64",
    os = "darwin",
    tars = [
        ":tools_tar_darwin_arm64",
        ":node_tar_darwin_arm64",
        ":python_tar_darwin_arm64",
        ":pnpm_tar",
        ":prettier_tar",
        ":homelab_cli_tar",
    ],
)

oci_image_index(
    name = "image",
    images = [
        ":image_linux_amd64",
        ":image_linux_arm64",
        ":image_darwin_arm64",
    ],
)

# Stamped tags for CI (branch + timestamp)
expand_template(
    name = "image_stamped_tags_ci",
    out = "image_stamped_ci.tags.txt",
    stamp_substitutions = {
        "{STABLE_BRANCH_TAG}": "{{STABLE_BRANCH_TAG}}",
        "{STABLE_IMAGE_TAG}": "{{STABLE_IMAGE_TAG}}",
    },
    template = [
        "{STABLE_BRANCH_TAG}",
        "{STABLE_IMAGE_TAG}",
    ],
)

# Stamped tags for local builds (timestamp only)
expand_template(
    name = "image_stamped_tags_local",
    out = "image_stamped_local.tags.txt",
    stamp_substitutions = {
        "{STABLE_IMAGE_TAG}": "{{STABLE_IMAGE_TAG}}",
    },
    template = ["{STABLE_IMAGE_TAG}"],
)

oci_push(
    name = "image.push",
    image = ":image",
    remote_tags = select({
        "//bazel/tools/oci:ci_build": "image_stamped_tags_ci",
        "//conditions:default": "image_stamped_tags_local",
    }),
    repository = "ghcr.io/jomcgi/homelab/bazel/tools/image",
    visibility = ["//bazel/images:__pkg__"],
)

semgrep_target_test(
    name = "python_deps_semgrep_test",
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":python_deps",
)

# Unit tests verifying that the pip deps declared in python_deps.py are
# importable and the key public APIs are constructible.
py_test(
    name = "python_deps_test",
    srcs = ["python_deps_test.py"],
    deps = [
        "@pip//httpx",
        "@pip//pytest",
        "@pip//typer",
    ],
)

semgrep_test(
    name = "python_deps_test_semgrep_test",
    srcs = ["python_deps_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    tags = ["semgrep"],
)

bzl_library(
    name = "multitool_tar",
    srcs = ["multitool_tar.bzl"],
    visibility = ["//visibility:public"],
)
