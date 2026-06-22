# gazelle:ignore
#
# CI-execution-only image for the BuildBuddy "Format check" action, published
# under the ci/ GHCR namespace (ghcr.io/jomcgi/homelab/ci/format). It is NOT a
# deployed runtime artifact. It lets the Format check run formatters +
# buildifier + the custom gazelle binary DIRECTLY, with no `bazel run` / RBE
# roundtrip.
#
# Single-arch (linux/amd64) by design: this image only ever executes on
# BuildBuddy's amd64 workflow pool, and the custom //:gazelle_binary links cgo
# (the rules_python gazelle plugin pulls in the tree-sitter C bindings) which
# cannot be cross-compiled to aarch64 here -- the repo has no aarch64 C++
# toolchain yet (see bazel/ocaml/platforms, the `no-arm64`-tagged examples).
# The dual-arch convention is for deployed images pulled onto mixed-arch cluster
# nodes; a CI-execution image has exactly one consumer arch. Because the whole
# build runs on the amd64 exec host, every tool is baked natively (no platform
# transition), so each binary is byte-identical to what
# `bazel run //bazel/tools/format:format` builds today: version parity by
# construction.

load("//bazel/tools/oci:apko_image.bzl", "apko_image")

package(default_visibility = ["//bazel/images:__pkg__"])

# --- Tool tars (all linux/amd64, baked natively) ---------------------------
# Each genrule drops one or more binaries into /usr/bin inside a tar that
# oci_image layers onto the apko base.

# Custom gazelle: classic bazel-contrib gazelle with the in-repo
# helm/wrangler/semgrep Go extensions compiled in -- the gazelle the format
# multirun runs today (NOT the aspect-gazelle release binary the tools image
# ships for local dev). cgo, hence amd64-only.
genrule(
    name = "gazelle_tar",
    srcs = ["//:gazelle_binary"],
    outs = ["gazelle.tar"],
    cmd = "\n".join([
        "mkdir -p tmp/usr/bin",
        "cp $(location //:gazelle_binary) tmp/usr/bin/gazelle",
        "chmod 0755 tmp/usr/bin/gazelle",
        "tar -C tmp -cf $@ .",
    ]),
)

# buildifier: the Starlark formatter the format multirun runs via Bazel. Not
# shipped in the tools image, so it must be baked here.
genrule(
    name = "buildifier_tar",
    srcs = ["@buildifier_prebuilt//:buildifier"],
    outs = ["buildifier.tar"],
    cmd = "\n".join([
        "mkdir -p tmp/usr/bin",
        "cp $(location @buildifier_prebuilt//:buildifier) tmp/usr/bin/buildifier",
        "chmod 0755 tmp/usr/bin/buildifier",
        "tar -C tmp -cf $@ .",
    ]),
)

# Standalone formatters ruff/shfmt/gofumpt, from the multitool repos -- the
# exact binaries fast-format.sh resolves from $PATH locally (the established
# "fast format" path).
genrule(
    name = "fmt_tar",
    srcs = [
        "@multitool.ruff.linux_x86_64//tools/ruff:linux_x86_64_executable",
        "@multitool.shfmt.linux_x86_64//tools/shfmt:linux_x86_64_executable",
        "@multitool.gofumpt.linux_x86_64//tools/gofumpt:linux_x86_64_executable",
    ],
    outs = ["fmt.tar"],
    cmd = "\n".join([
        "mkdir -p tmp/usr/bin",
        "cp $(location @multitool.ruff.linux_x86_64//tools/ruff:linux_x86_64_executable) tmp/usr/bin/ruff",
        "cp $(location @multitool.shfmt.linux_x86_64//tools/shfmt:linux_x86_64_executable) tmp/usr/bin/shfmt",
        "cp $(location @multitool.gofumpt.linux_x86_64//tools/gofumpt:linux_x86_64_executable) tmp/usr/bin/gofumpt",
        "chmod 0755 tmp/usr/bin/ruff tmp/usr/bin/shfmt tmp/usr/bin/gofumpt",
        "tar -C tmp -cf $@ .",
    ]),
)

# Node.js runtime (hermetic), needed only so prettier.cjs can launch.
genrule(
    name = "node_tar",
    srcs = ["@nodejs_linux_amd64//:node_bin"],
    outs = ["node.tar"],
    cmd = "\n".join([
        "mkdir -p tmp/usr/bin",
        "cp $(location @nodejs_linux_amd64//:node_bin) tmp/usr/bin/node",
        "chmod 0755 tmp/usr/bin/node",
        "tar -C tmp -cf $@ .",
    ]),
)

# prettier: the same pnpm-managed package the format multirun and the tools
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

# --- Single-arch image -----------------------------------------------------
# apko provides the Wolfi userland; the tool tars are layered on top. archs is
# pinned to amd64 (see the header) so the macro emits a lone oci_image plus one
# format.push, not a multi-platform index.
apko_image(
    name = "format",
    archs = ["amd64"],
    config = "apko.yaml",
    contents = "@ci_format_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/ci/format",
    tars = [
        ":gazelle_tar",
        ":buildifier_tar",
        ":fmt_tar",
        ":node_tar",
        ":prettier_tar",
    ],
)
