# gazelle:ignore
load("@aspect_bazel_lib//lib:jq.bzl", "jq")
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Both arms use identical model metadata apart from leaving tool_mode to the flag.
jq(
    name = "trial_catalog",
    srcs = ["@factory_codex_catalog//file"],
    out = "trial-models.json",
    filter = """if any(.models[]; .slug == "gpt-6.2-astra" and .tool_mode == "code_mode_only") then del(.models[] | select(.slug == "gpt-6.2-astra").tool_mode) else error("unexpected trial model metadata") end""",
)

_BIN = "@factory_codex_binaries//:usr/local/bin/"

_NATIVE = [
    "codex",
    "codex-code-mode-host",
    "codex-exec-server",
]

tar(
    name = "runtime",
    srcs = [
        "codex.sh",
        ":trial_catalog",
        _BIN + "kagent-codex",
    ] + [_BIN + b for b in _NATIVE],
    mtree = [
        "./etc/codex/trial-models.json type=file content=$(execpath :trial_catalog) mode=0444 uid=0 gid=0",
        "./usr/local/bin/codex type=file content=$(execpath codex.sh) mode=0555 uid=0 gid=0",
        "./usr/local/bin/kagent-codex type=file content=$(execpath " + _BIN + "kagent-codex) mode=0555 uid=0 gid=0",
    ] + [
        "./usr/local/libexec/codex/" + b + " type=file content=$(execpath " + _BIN + b + ") mode=0555 uid=0 gid=0"
        for b in _NATIVE
    ],
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@factory_codex_mode_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/codex-code-mode",
    tars = [":runtime"],
)
