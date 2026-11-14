# gazelle:ignore
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

exec_filegroup(
    name = "web_dist",
    src = "//projects/monolith/web/grimoire:factory_build",
)

genrule(
    name = "static_tar",
    srcs = ["server.mjs", ":web_dist"],
    outs = ["static.tar"],
    cmd = """
        mkdir -p "$(@D)/static-root/app/build/static"
        cp -rL $(location :web_dist)/* "$(@D)/static-root/app/build/static/"
        cp $(location server.mjs) "$(@D)/static-root/app/build/index.js"
        echo '{"type":"module"}' > "$(@D)/static-root/app/package.json"
        tar -C "$(@D)/static-root" --owner=65532 --group=65532 -cf $@ app
    """,
)

# Reuse the locked, nonroot Node static-server runtime; no new APK packages.
apko_image(
    name = "image",
    arm64 = False,
    config = "//projects/monolith/frontend:apko.yaml",
    contents = "@monolith_frontend_lock//:contents",
    tars = [":static_tar"],
)
