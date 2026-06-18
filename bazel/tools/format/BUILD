"""BUILD definition for the formatter binary

This is in its own package because it has so many loading-time symbols,
we don't want to trigger eager fetches of these for builds that don't want to run format.
"""

load("@aspect_rules_js//js:defs.bzl", "js_library")
load("@aspect_rules_lint//format:defs.bzl", "format_multirun")
load("@npm//:prettier/package_json.bzl", prettier = "bin")
load("@rules_multirun//:defs.bzl", "multirun")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

# gazelle:exclude
package(default_visibility = ["//:__subpackages__"])

js_library(
    name = "prettierrc",
    srcs = ["prettier.config.cjs"],
)

prettier.prettier_binary(
    name = "prettier",
    # Include this js_library and its dependencies in the runfiles (runtime dependencies)
    data = [":prettierrc"],
    # Allow the binary to be run outside bazel
    env = {"BAZEL_BINDIR": "."},
    fixed_args = [
        # `require` statements in the config file will be resolved relative to its location
        # Therefore to make it hermetic, prettier must be pointed at the copy of the config file
        # in the runfiles folder rather than the one in the source folder.
        "--config=\"$$JS_BINARY__RUNFILES\"/$(rlocationpath :prettierrc)",
        # default log level is "log" which spams on success
        # https://prettier.io/docs/en/cli.html#--log-level
        "--log-level=warn",
    ],
)

format_multirun(
    name = "format_code",
    go = "@aspect_rules_lint//format:gofumpt",
    javascript = ":prettier",
    python = "@aspect_rules_lint//format:ruff",
    rust = "@rules_rust//tools/rustfmt:upstream_rustfmt",
    shell = "@aspect_rules_lint//format:shfmt",
    starlark = "@buildifier_prebuilt//:buildifier",
)

multirun(
    name = "format",
    commands = [
        "//bazel/images:generate-home-cluster",
        "//bazel/images:generate-push-all",
        "//bazel/images:generate-push-all-pages",
        "//projects/monolith:generate-routes",
        ":format_code",
        "//:gazelle",
    ],
    jobs = 10,
)

# Update apko lock files
sh_binary(
    name = "update_apko_locks",
    srcs = ["update-apko-locks.sh"],
)

# Update Python requirements lock files
sh_binary(
    name = "update_python_requirements",
    srcs = ["update-python-requirements.sh"],
)

# Validate apko config files
sh_binary(
    name = "validate_apko_configs",
    srcs = ["validate-apko-configs.sh"],
)

# Fast format script - builds once, runs in parallel
# Used by both pre-commit and CI for identical formatting
sh_binary(
    name = "fast_format",
    srcs = ["fast-format.sh"],
)

# Atlas migration checksum validator — used by sh_test targets in chart BUILD files
exports_files(["validate-atlas-sum.sh"])
