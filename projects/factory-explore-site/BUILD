# gazelle:ignore
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

exec_filegroup(
    name = "web_dist",
    src = "//projects/factory-explore/ui:build",
)

genrule(
    name = "site_tar",
    srcs = [
        "wrangler.jsonc",
        ":web_dist",
    ],
    outs = ["site.tar"],
    cmd = """
        mkdir -p "$(@D)/site-root/app/site/dist"
        cp -rL $(location :web_dist)/. "$(@D)/site-root/app/site/dist/"
        cp $(location wrangler.jsonc) "$(@D)/site-root/app/site/wrangler.jsonc"
        test -z "$$(find "$(@D)/site-root" -type f -size +26214400c -print -quit)"
        tar -C "$(@D)/site-root" --owner=65532 --group=65532 -cf $@ app
    """,
)

apko_image(
    name = "image",
    arm64 = False,
    config = "//projects/jomcgi-site:apko.yaml",
    contents = "@jomcgi_site_lock//:contents",
    tars = [
        ":site_tar",
        "//projects/jomcgi-site/wrangler:layers",
    ],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
