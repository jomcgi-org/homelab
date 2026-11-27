load("//bazel/tools/pytest:defs.bzl", "py_test")

# The pinned libraries artifact pages load from
# https://artifact.jomcgi.dev/_lib/. The connector image
# (projects/factory/connector/image) puts them and the self-test page in
# /usr/share/bosun-libs; the bosun-libs Job uploads them to the bosun-libs
# bucket. Change a version here, there, in MODULE.bazel and in the pages
# together.
LIBS = [
    "@discord_lib_mermaid//:dist/mermaid.min.js",
    "@discord_lib_vega//:build/vega.min.js",
    "@discord_lib_vega_lite//:build/vega-lite.min.js",
    "@discord_lib_vega_embed//:build/vega-embed.min.js",
]

exports_files(
    ["selftest/architecture.html"],
    visibility = ["//projects/factory/connector/image:__pkg__"],
)

py_test(
    name = "sri_test",
    srcs = ["sri_test.py"],
    data = [
        "selftest/architecture.html",
        "//projects/factory/discord/artifacts:template.html",
    ] + LIBS,
    env = {
        "LIB_FILES": " ".join(["$(rootpath %s)" % lib for lib in LIBS]),
        "LIB_NAMES": "mermaid-12.3.0.min.js vega-6.4.0.min.js vega-lite-6.5.0.min.js vega-embed-7.3.0.min.js",
        "PAGES": "$(rootpath //projects/factory/discord/artifacts:template.html) $(rootpath selftest/architecture.html)",
    },
    deps = ["@pip//pytest"],
)
