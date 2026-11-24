load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "render_test",
    srcs = ["render_test.sh"],
    args = [
        "$(rootpath @multitool//tools/helm)",
        "$(rootpath @cloudnative_pg_chart_0_29_0//file)",
        "$(rootpath //projects:platform/cloudnative-pg/deploy/values.yaml)",
        "$(rootpath //projects:platform-factory/cloudnative-pg/values.yaml)",
        "$(rootpath operator-objects.awk)",
        "$(rootpath //projects:platform-factory/cloudnative-pg/resources/upstream/operator.yaml)",
    ],
    data = [
        "operator-objects.awk",
        "//projects:platform-factory/cloudnative-pg/resources/upstream/operator.yaml",
        "//projects:platform-factory/cloudnative-pg/values.yaml",
        "//projects:platform/cloudnative-pg/deploy/values.yaml",
        "@cloudnative_pg_chart_0_29_0//file",
        "@multitool//tools/helm",
    ],
)
