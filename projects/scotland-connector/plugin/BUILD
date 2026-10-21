load("//bazel/tools/pytest:defs.bzl", "py_test")

# The Claude Code and OpenAI plugin for the connector, staged here and
# copied to its own repository for the directory listings (#7255).
py_test(
    name = "plugin_test",
    srcs = ["plugin_test.py"],
    data = [
        ".claude-plugin/marketplace.json",
        ".claude-plugin/plugin.json",
        ".mcp.json",
        "mcp.json",
        "plugin.json",
        "skills/scotland-trips/SKILL.md",
    ],
    deps = ["@pip//pytest"],
)
