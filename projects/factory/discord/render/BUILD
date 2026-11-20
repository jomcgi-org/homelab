load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("@rules_rust//rust:defs.bzl", "rust_binary", "rust_test")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

exports_files([
    "Cargo.lock",
    "Cargo.toml",
])

rust_binary(
    name = "factory-discord-render",
    srcs = ["src/main.rs"],
    crate_name = "factory_discord_render",
    edition = "2021",
    deps = [
        "@discord_render_crates//:serde_json",
        "@discord_render_crates//:tiny_http",
    ],
)

rust_test(
    name = "input_test",
    crate = ":factory-discord-render",
)

tar(
    name = "executables",
    srcs = [
        ":factory-discord-render",
        "@discord_render_mmdr//:mmdr",
        "@discord_render_vl_convert//:bin/vl-convert",
    ],
    mtree = [
        "./usr/local/bin/factory-discord-render type=file content=$(execpath :factory-discord-render) mode=0755 uid=0 gid=0",
        "./usr/local/bin/mmdr type=file content=$(execpath @discord_render_mmdr//:mmdr) mode=0755 uid=0 gid=0",
        "./usr/local/bin/vl-convert type=file content=$(execpath @discord_render_vl_convert//:bin/vl-convert) mode=0755 uid=0 gid=0",
    ],
)

pkg_tar(
    name = "fonts",
    srcs = ["@discord_render_fonts//:fonts"],
    mode = "0644",
    package_dir = "/usr/share/fonts/truetype/dejavu",
)

tar(
    name = "licenses",
    srcs = [
        "goldens/LICENSE",
        "@discord_render_fonts//:LICENSE",
        "@discord_render_vl_convert//:bin/LICENSE",
        "@discord_render_vl_convert//:bin/thirdparty_font.md",
        "@discord_render_vl_convert//:bin/thirdparty_javascript.md",
        "@discord_render_vl_convert//:bin/thirdparty_rust.yaml",
    ],
    mtree = [
        "./usr/share/doc/mmdr/LICENSE type=file content=$(execpath goldens/LICENSE)",
        "./usr/share/doc/dejavu/LICENSE type=file content=$(execpath @discord_render_fonts//:LICENSE)",
    ] + [
        "./usr/share/doc/vl-convert/%s type=file content=$(execpath @discord_render_vl_convert//:bin/%s)" % (file, file)
        for file in [
            "LICENSE",
            "thirdparty_font.md",
            "thirdparty_javascript.md",
            "thirdparty_rust.yaml",
        ]
    ],
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@discord_render_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/discord/render",
    tars = [
        ":executables",
        ":fonts",
        ":licenses",
    ],
)
