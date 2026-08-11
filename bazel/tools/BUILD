load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

"""
Build supporting tools.

The tools subpackage is used to define build rules and configurations for tools
used throughout the repository. Firstparty tools not required for build support
should live elsewhere.

Of special note is that we're using the `bazel_env.bzl` tool to re-export tools
used under Bazel as a `bin/` tree which can be added to the `$PATH`. The
included `.envrc` automates this for you.

"""

load("@bazel_env.bzl", "bazel_env")
load("@bazelrc-preset.bzl", "bazelrc_preset")
load("@multitool//:tools.bzl", MULTITOOL_TOOLS = "TOOLS")
load("@npm//:defs.bzl", "npm_link_all_packages")
load("@rules_python//python/entry_points:py_console_script_binary.bzl", "py_console_script_binary")

package(default_visibility = ["//visibility:public"])

bazelrc_preset(
    name = "preset",
    doc_link_template = "https://registry.build/flag/bazel?filter={flag}",
)

py_console_script_binary(
    name = "copier",
    pkg = "@pip//copier",
)

npm_link_all_packages(name = "node_modules")

bazel_env(
    name = "bazel_env",
    toolchains = {
        "python": "@rules_python//python:current_py_toolchain",
        "nodejs": "@nodejs_toolchains//:resolved_toolchain",
        "rust": "@rules_rust//rust/toolchain:current_rust_toolchain",
    },
    tools = {
        # Mapping from tool binary name ($PATH entry) to Bazel label
        "buildifier": "@buildifier_prebuilt//:buildifier",
        "format": "//bazel/tools/format:fast_format",
        "cargo": "@rules_rust//tools/upstream_wrapper:cargo",
        "copier": ":copier",
        "go": "@rules_go//go",
        "scaffold": "@com_github_hay_kot_scaffold//:scaffold",
        "node": "$(NODE_PATH)",
        "pnpm": "@pnpm",
        "python": "$(PYTHON3)",
        # Multitool tools (excluding gazelle - use `bazel run //:gazelle` instead)
        "aspect": MULTITOOL_TOOLS["aspect"],
        "argocd": MULTITOOL_TOOLS["argocd"],
        "buildozer": MULTITOOL_TOOLS["buildozer"],
        "ibazel": MULTITOOL_TOOLS["ibazel"],
        "op": MULTITOOL_TOOLS["op"],
        "starpls": MULTITOOL_TOOLS["starpls"],
        "helm": MULTITOOL_TOOLS["helm"],
        "kind": MULTITOOL_TOOLS["kind"],
        "crane": MULTITOOL_TOOLS["crane"],
        "gh": MULTITOOL_TOOLS["gh"],
        "gofumpt": MULTITOOL_TOOLS["gofumpt"],
        "ruff": MULTITOOL_TOOLS["ruff"],
        "shfmt": MULTITOOL_TOOLS["shfmt"],
        # HuggingFace model tools
        "hf_lock": ":hf_lock",
        # Agent tools
        "agent-run": "//bazel/tools/agent-run",
    },
)

sh_binary(
    name = "workspace_status",
    srcs = ["workspace_status.sh"],
)

sh_binary(
    name = "hf_lock",
    srcs = ["hf_lock.sh"],
)

sh_binary(
    name = "help",
    srcs = ["help.sh"],
)
