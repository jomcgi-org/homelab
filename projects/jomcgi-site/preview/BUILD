# The PR preview uploader (README "PR previews"): checks a session's built
# site and uploads it as preview alias pr-<N> of Worker jomcgi-site-dev.
# preview.sh is the session side; sessions run it directly from their
# checkout. Hand-maintained.
# gazelle:ignore
load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_image_layer", "js_test")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

js_binary(
    name = "preview",
    data = [
        "archive.mjs",
        "github.mjs",
        "uploader.mjs",
    ],
    entry_point = "main.mjs",
)

js_image_layer(
    name = "layers",
    binary = ":preview",
    root = "/app",
    visibility = ["//projects/jomcgi-site:__pkg__"],
)

js_test(
    name = "archive_test",
    data = [
        "archive.mjs",
        "tar-fixture.mjs",
    ],
    entry_point = "archive.test.mjs",
)

js_test(
    name = "uploader_test",
    data = [
        "archive.mjs",
        "github.mjs",
        "tar-fixture.mjs",
        "uploader.mjs",
    ],
    entry_point = "uploader.test.mjs",
)

# preview.sh with fake gh, kubectl, pnpm and curl: one comment, its own
# comment edited, refusals and unexpected URLs never posted.
sh_test(
    name = "preview_test",
    srcs = ["preview_test.sh"],
    data = [
        "preview.sh",
        "@nodejs_linux_amd64//:node_bin",
    ],
    env = {"NODE_BIN": "$(rootpath @nodejs_linux_amd64//:node_bin)"},
)
