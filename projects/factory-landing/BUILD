load("@aspect_rules_js//js:defs.bzl", "js_library", "js_test")

# gazelle:ignore
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# rules_js copies sources in their owning package; consumers use JsInfo deps.
js_library(
    name = "server",
    srcs = ["server.mjs"],
    types = ["server.d.mts"],
    visibility = ["//projects/factory-landing/web:__pkg__"],
)

js_test(
    name = "server_test",
    data = [
        ":server",
        "//projects/factory-landing/web:paging",
    ],
    entry_point = "server.test.mjs",
)

exec_filegroup(
    name = "web_dist",
    src = "//projects/factory-landing/web:build",
)

genrule(
    name = "static_tar",
    srcs = [
        "server.mjs",
        ":web_dist",
    ],
    outs = ["static.tar"],
    cmd = """
        mkdir -p "$(@D)/static-root/app/build/static"
        cp -rL $(location :web_dist)/* "$(@D)/static-root/app/build/static/"
        cp $(location server.mjs) "$(@D)/static-root/app/build/index.js"
        echo '{"type":"module"}' > "$(@D)/static-root/app/package.json"
        tar -C "$(@D)/static-root" --owner=65532 --group=65532 -cf $@ app
    """,
)

# Standalone, locked nonroot Node runtime; no monolith build inputs.
apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@factory_landing_lock//:contents",
    tars = [":static_tar"],
)

# Indexed through //:repo_docs so the factory does not modify the monolith.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//:__pkg__"],
)
