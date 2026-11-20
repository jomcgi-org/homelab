load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# health_exporter.py and cloudflare_exporter.py ship in ConfigMaps, not as
# Bazel binaries. The factory-public chart test checks its copy of
# cloudflare_exporter.py against this one.
# gazelle:exclude health_exporter.py
# gazelle:exclude cloudflare_exporter.py

exports_files(["cloudflare_exporter.py"])

# The alert rules' unit tests: promtool evaluates rules.yml against the
# series in rules_test.yml (1 min rule grid) and rules_30s_test.yml (the
# production 30 s grid). pr-checks runs it when any of its inputs changes
# (bazel/tools/ci/affected-targets.sh).
sh_test(
    name = "rules_test",
    srcs = ["rules_test.sh"],
    args = [
        "$(rootpath @promtool_3_15_1_linux_amd64//:binary)",
        "$(rootpath rules_test.yml)",
        "$(rootpath rules_30s_test.yml)",
    ],
    data = [
        "rules.yml",
        "rules_30s_test.yml",
        "rules_test.yml",
        "@promtool_3_15_1_linux_amd64//:binary",
    ],
)

py_test(
    name = "health_exporter_test",
    srcs = ["health_exporter_test.py"],
    data = [
        "health_exporter.py",
        "@promtool_3_15_1_linux_amd64//:binary",
    ],
    env = {"PROMTOOL": "$(rootpath @promtool_3_15_1_linux_amd64//:binary)"},
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = ["@pip//pytest"],
)

py_test(
    name = "cloudflare_exporter_test",
    srcs = ["cloudflare_exporter_test.py"],
    data = [
        "cloudflare_exporter.py",
        "@promtool_3_15_1_linux_amd64//:binary",
    ],
    env = {"PROMTOOL": "$(rootpath @promtool_3_15_1_linux_amd64//:binary)"},
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = ["@pip//pytest"],
)

filegroup(
    name = "kustomize_base",
    srcs = glob(
        [
            "*.yaml",
            "*.yml",
        ],
        exclude = [
            "rules_30s_test.yml",
            "rules_test.yml",
        ],
    ) + [
        "cloudflare_exporter.py",
        "health_exporter.py",
    ],
    visibility = ["//projects/platform-factory/prometheus/stack:__pkg__"],
)
