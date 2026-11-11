load("//bazel/tools/pytest:defs.bzl", "py_test")

# The Claude Code and OpenAI plugin for the connector. It stays in this
# repository; see the Listings section of ../ARCHITECTURE.md (#7255).
py_test(
    name = "plugin_test",
    srcs = ["plugin_test.py"],
    data = [
        ".claude-plugin/plugin.json",
        ".mcp.json",
        "LICENSE",
        "assets/icon.png",
        "assets/icon-dark.png",
        "mcp.json",
        "plugin.json",
        "skills/scotland-trips/SKILL.md",
    ],
)

# The README and the skill are indexed with the repo docs.
# See //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(["**/*.md"]),
    visibility = ["//projects/monolith:__pkg__"],
)
