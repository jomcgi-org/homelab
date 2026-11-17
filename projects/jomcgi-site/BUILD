# gazelle:ignore
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

exec_filegroup(
    name = "web_dist",
    src = "//projects/jomcgi-site/web:build",
)

# /app/site holds wrangler.jsonc and the built pages at its assets directory.
genrule(
    name = "site_tar",
    srcs = [
        "wrangler.jsonc",
        ":web_dist",
    ],
    outs = ["site.tar"],
    cmd = """
        mkdir -p "$(@D)/site-root/app/site/web/dist"
        cp -rL $(location :web_dist)/. "$(@D)/site-root/app/site/web/dist/"
        cp $(location wrangler.jsonc) "$(@D)/site-root/app/site/wrangler.jsonc"
        tar -C "$(@D)/site-root" --owner=65532 --group=65532 -cf $@ app
    """,
)

# The site's Job image: `wrangler deploy --env <stage>` with the pinned CLI,
# and the edge apply step at /app/projects/jomcgi-site/edge/apply.
apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@jomcgi_site_lock//:contents",
    tars = [
        ":site_tar",
        "//projects/jomcgi-site/edge:layers",
        "//projects/jomcgi-site/wrangler:layers",
    ],
)

filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//:__pkg__"],
)
