# gazelle:ignore
#
# CI-execution-only image for the BuildBuddy "Format check" action, published
# under the ci/ GHCR namespace (ghcr.io/jomcgi/homelab/ci/format). It is NOT a
# deployed runtime artifact. It lets the Format check run formatters +
# buildifier + the custom gazelle binary DIRECTLY, with no `bazel run` / RBE
# roundtrip.
#
# Design: a minimal Wolfi userland (apko.yaml) with every actual tool layered
# on top as a Bazel-built tar, so each baked binary is byte-identical to what
# `bazel run //bazel/tools/format:format` builds today. Version parity is
# guaranteed by construction (same Bazel artifacts), not by matching Wolfi
# package versions to Bazel-pinned ones.
#
# Per-architecture handling: apko_image layers `multiarch_tars` onto per-arch
# bases using {base}_amd64 / {base}_arm64 suffixes, so every arch-specific tool
# is provided as both. ruff/shfmt/gofumpt come from the per-arch multitool repos
# (the same binaries fast-format.sh uses locally); the cgo gazelle binary and
# the buildifier binary are produced per-arch via a rules_go platform
# transition; node comes from the hermetic rules_nodejs per-arch repos. prettier
# is arch-independent JS and is layered once via `tars`.

load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

package(default_visibility = ["//bazel/images:__pkg__"])

# --- Custom gazelle binary (//:gazelle_binary) -----------------------------
# Classic bazel-contrib gazelle with the in-repo helm/wrangler/semgrep Go
# extensions compiled in. This is the gazelle the format multirun runs today
# (NOT the aspect-gazelle release binary that the tools image ships for local
# dev). Transitioned per-arch because it is built by rules_go (cgo).
[
    platform_transition_filegroup(
        name = "gazelle_bin_" + arch,
        srcs = ["//:gazelle_binary"],
        target_platform = "@rules_go//go/toolchain:linux_" + arch,
    )
    for arch in [
        "amd64",
        "arm64",
    ]
]

[
    genrule(
        name = "gazelle_tar_" + arch,
        srcs = [":gazelle_bin_" + arch],
        outs = ["gazelle_" + arch + ".tar"],
        cmd = "\n".join([
            "mkdir -p tmp/usr/bin",
            "cp $(location :gazelle_bin_" + arch + ") tmp/usr/bin/gazelle",
            "chmod 0755 tmp/usr/bin/gazelle",
            "tar -C tmp -cf $@ .",
        ]),
    )
    for arch in [
        "amd64",
        "arm64",
    ]
]

# --- buildifier (@buildifier_prebuilt//:buildifier) ------------------------
# The Starlark formatter the format multirun runs via Bazel. Not shipped in the
# tools image, so it must be baked here. Transitioned per-arch to select the
# matching prebuilt download.
[
    platform_transition_filegroup(
        name = "buildifier_bin_" + arch,
        srcs = ["@buildifier_prebuilt//:buildifier"],
        target_platform = "@rules_go//go/toolchain:linux_" + arch,
    )
    for arch in [
        "amd64",
        "arm64",
    ]
]

[
    genrule(
        name = "buildifier_tar_" + arch,
        srcs = [":buildifier_bin_" + arch],
        outs = ["buildifier_" + arch + ".tar"],
        cmd = "\n".join([
            "mkdir -p tmp/usr/bin",
            "cp $(location :buildifier_bin_" + arch + ") tmp/usr/bin/buildifier",
            "chmod 0755 tmp/usr/bin/buildifier",
            "tar -C tmp -cf $@ .",
        ]),
    )
    for arch in [
        "amd64",
        "arm64",
    ]
]

# --- Standalone formatters: ruff, shfmt, gofumpt ---------------------------
# Sourced from the per-arch multitool repos (the exact binaries fast-format.sh
# resolves from $PATH locally, which is the established "fast format" path).
_MULTITOOL_CPU = {
    "amd64": "x86_64",
    "arm64": "arm64",
}

[
    genrule(
        name = "fmt_tar_" + arch,
        srcs = [
            "@multitool.ruff.linux_{cpu}//tools/ruff:linux_{cpu}_executable".format(cpu = cpu),
            "@multitool.shfmt.linux_{cpu}//tools/shfmt:linux_{cpu}_executable".format(cpu = cpu),
            "@multitool.gofumpt.linux_{cpu}//tools/gofumpt:linux_{cpu}_executable".format(cpu = cpu),
        ],
        outs = ["fmt_" + arch + ".tar"],
        cmd = "\n".join([
            "mkdir -p tmp/usr/bin",
            "cp $(location @multitool.ruff.linux_{cpu}//tools/ruff:linux_{cpu}_executable) tmp/usr/bin/ruff".format(cpu = cpu),
            "cp $(location @multitool.shfmt.linux_{cpu}//tools/shfmt:linux_{cpu}_executable) tmp/usr/bin/shfmt".format(cpu = cpu),
            "cp $(location @multitool.gofumpt.linux_{cpu}//tools/gofumpt:linux_{cpu}_executable) tmp/usr/bin/gofumpt".format(cpu = cpu),
            "chmod 0755 tmp/usr/bin/ruff tmp/usr/bin/shfmt tmp/usr/bin/gofumpt",
            "tar -C tmp -cf $@ .",
        ]),
    )
    for arch, cpu in _MULTITOOL_CPU.items()
]

# --- Node.js runtime (per-arch, hermetic) ----------------------------------
# Needed only so prettier.cjs can launch (`#!/usr/bin/env node`).
[
    genrule(
        name = "node_tar_" + arch,
        srcs = ["@nodejs_linux_" + arch + "//:node_bin"],
        outs = ["node_" + arch + ".tar"],
        cmd = "\n".join([
            "mkdir -p tmp/usr/bin",
            "cp $(location @nodejs_linux_" + arch + "//:node_bin) tmp/usr/bin/node",
            "chmod 0755 tmp/usr/bin/node",
            "tar -C tmp -cf $@ .",
        ]),
    )
    for arch in [
        "amd64",
        "arm64",
    ]
]

# --- prettier (arch-independent JS) ----------------------------------------
# The same pnpm-managed prettier package the format multirun and the tools
# image use. Placed at /usr/local/lib/node_modules/prettier with a /usr/bin
# symlink, mirroring bazel/tools/image:prettier_tar.
genrule(
    name = "prettier_tar",
    srcs = ["//:node_modules/prettier"],
    outs = ["prettier.tar"],
    cmd = """
        set -e
        WORK=$$(mktemp -d)
        DEST="$$WORK/usr/local/lib/node_modules/prettier"
        mkdir -p "$$DEST"
        SRC_DIR=$$(echo '$(locations //:node_modules/prettier)' | tr ' ' '\\n' | grep 'prettier$$' | head -1)
        cp -rL "$$SRC_DIR/." "$$DEST/"
        chmod 755 "$$DEST/bin/prettier.cjs"
        mkdir -p "$$WORK/usr/bin"
        ln -s "../local/lib/node_modules/prettier/bin/prettier.cjs" "$$WORK/usr/bin/prettier"
        tar -cf $@ -C "$$WORK" .
        rm -rf "$$WORK"
    """,
)

apko_image(
    name = "format",
    config = "apko.yaml",
    contents = "@ci_format_lock//:contents",
    multiarch_tars = [
        ":gazelle_tar",
        ":buildifier_tar",
        ":fmt_tar",
        ":node_tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/ci/format",
    tars = [":prettier_tar"],
)
