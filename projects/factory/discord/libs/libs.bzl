"""The files served at https://artifact.jomcgi.dev/_lib/, by published name.

The connector image puts each in /usr/share/bosun-libs/<name>; the
bosun-libs Job uploads them to the bosun-libs bucket. A library's name
carries its version: pages published earlier keep loading the version they
name, so add a new name for a new version instead of replacing a file.
"""

LIBS = {
    "mermaid-12.3.0.min.js": "@discord_lib_mermaid//:dist/mermaid.min.js",
    "vega-6.4.0.min.js": "@discord_lib_vega//:build/vega.min.js",
    "vega-lite-6.5.0.min.js": "@discord_lib_vega_lite//:build/vega-lite.min.js",
    "vega-embed-7.3.0.min.js": "@discord_lib_vega_embed//:build/vega-embed.min.js",
    "lucide-1.72.0.min.js": "@discord_lib_lucide//:dist/umd/lucide.min.js",
    "inter-5.3.0-latin-wght-normal.woff2": "@discord_lib_inter//:files/inter-latin-wght-normal.woff2",
    "schibsted-grotesk-5.3.0-latin-wght-normal.woff2": "@discord_lib_schibsted_grotesk//:files/schibsted-grotesk-latin-wght-normal.woff2",
    "house-1.css": "//projects/factory/discord/libs:house/house-1.css",
    "house-1.js": "//projects/factory/discord/libs:house/house-1.js",
    "house-2.css": "//projects/factory/discord/libs:house/house-2.css",
    "house-2.js": "//projects/factory/discord/libs:house/house-2.js",
    "selftest/architecture.html": "//projects/factory/discord/libs:selftest/architecture.html",
    "licenses/lucide.txt": "@discord_lib_lucide//:LICENSE",
    "licenses/inter.txt": "@discord_lib_inter//:LICENSE",
    "licenses/schibsted-grotesk.txt": "@discord_lib_schibsted_grotesk//:LICENSE",
}
