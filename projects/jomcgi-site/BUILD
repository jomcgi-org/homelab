# gazelle:ignore
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

exec_filegroup(
    name = "web_dist",
    src = "//projects/jomcgi-site/web:build",
)

# /app/site holds wrangler.jsonc, run.sh and the built pages at its assets directory.
genrule(
    name = "site_tar",
    srcs = [
        "run.sh",
        "wrangler.jsonc",
        ":web_dist",
    ],
    outs = ["site.tar"],
    cmd = """
        mkdir -p "$(@D)/site-root/app/site/web/dist"
        cp -rL $(location :web_dist)/. "$(@D)/site-root/app/site/web/dist/"
        cp $(location wrangler.jsonc) "$(@D)/site-root/app/site/wrangler.jsonc"
        cp $(location run.sh) "$(@D)/site-root/app/site/run.sh"
        chmod 0755 "$(@D)/site-root/app/site/run.sh"
        tar -C "$(@D)/site-root" --owner=65532 --group=65532 -cf $@ app
    """,
)

# The site's Job image: run.sh (data snapshot, then `wrangler deploy --env
# <stage>` with the pinned CLI), and the edge apply step at
# /app/projects/jomcgi-site/edge/apply.
apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@jomcgi_site_lock//:contents",
    tars = [
        ":site_tar",
        "//projects/jomcgi-site/data:layers",
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

# run.sh assembles the site with fresh data, then deploys it (HOME-209).
sh_test(
    name = "run_test",
    srcs = ["run_test.sh"],
    data = ["run.sh"],
)
