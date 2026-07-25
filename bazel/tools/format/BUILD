"""BUILD definition for the formatter binary

This is in its own package because it has so many loading-time symbols,
we don't want to trigger eager fetches of these for builds that don't want to run format.
"""

load("@aspect_rules_js//js:defs.bzl", "js_library")
load("@aspect_rules_lint//format:defs.bzl", "format_multirun")
load("@npm//:prettier/package_json.bzl", prettier = "bin")
load("@rules_multirun//:defs.bzl", "multirun")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

# gazelle:exclude
package(default_visibility = ["//:__subpackages__"])

# Hermetic tests for the migration-ordering guard (no git; base versions are
# overridden via MIGRATION_BASE_VERSIONS inside the test).
sh_test(
    name = "check_migration_ordering_test",
    srcs = ["check-migration-ordering_test.sh"],
    data = ["check-migration-ordering.sh"],
)

js_library(
    name = "prettierrc",
    srcs = ["prettier.config.cjs"],
    # Plugin + peer must be in runfiles so require("prettier-plugin-svelte")
    # from the config resolves under the hermetic prettier binary.
    deps = [
        "//:node_modules/prettier-plugin-svelte",
        "//:node_modules/svelte",
    ],
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

# aspect_rules_lint has no Svelte dialect; format tracked *.svelte via the same
# hermetic prettier (plugin loaded from :prettierrc).
sh_binary(
    name = "format_svelte",
    srcs = ["format-svelte.sh"],
    data = [":prettier"],
)

# All committed-artifact generators live in ONE list (run-generators.sh), shared
# with fast-format.sh, so the local and CI format paths cannot drift.
sh_binary(
    name = "run_generators",
    srcs = ["run-generators.sh"],
)

multirun(
    name = "format",
    commands = [
        ":run_generators",
        ":format_code",
        ":format_svelte",
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
