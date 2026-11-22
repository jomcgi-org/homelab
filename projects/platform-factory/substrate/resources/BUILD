load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# gazelle:python_generation_mode file
# The Job's script and pins run in a container, not under Bazel; the test reads
# them through the glob below.
# gazelle:exclude microvm-assets

exports_files(
    ["network-policy.yaml"],
    visibility = [
        "//projects/factory/chart:__pkg__",
        "//projects/platform-factory/network-policy/tests:__pkg__",
    ],
)

py_venv_binary(
    name = "render_egress_gateway",
    srcs = ["render_egress_gateway.py"],
    data = [
        "atenet-egress-config.yaml",
        "egress-scan/gateway-patch.yaml",
        "egress-scan/gateway-policy.yaml",
    ],
    main = "render_egress_gateway.py",
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pyyaml"],
)

py_test(
    name = "resources_test",
    srcs = ["resources_test.py"],
    data = glob([
        "*.yaml",
        "egress-scan/*.yaml",
        "microvm-assets/*",
        "patches/*.yaml",
        "upstream/**",
    ]) + [
        "//projects/platform-factory/substrate/credential-provider:image_digest",
        "//projects/platform-factory/substrate/egress-scan/chart:image_values",
        "//projects/platform-factory/substrate/egress-scan/deploy:application.yaml",
        "//projects/platform-factory/substrate/egress-scan/deploy:manifests/all.yaml",
        "@multitool//tools/kustomize",
    ],
    env = {
        "KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)",
        "PROVIDER_DIGEST": "$(rootpath //projects/platform-factory/substrate/credential-provider:image_digest)",
        "SCANNER_APPLICATION": "$(rootpath //projects/platform-factory/substrate/egress-scan/deploy:application.yaml)",
        "SCANNER_MANIFEST": "$(rootpath //projects/platform-factory/substrate/egress-scan/deploy:manifests/all.yaml)",
        "SCANNER_VALUES": "$(rootpath //projects/platform-factory/substrate/egress-scan/chart:image_values)",
    },
    deps = ["@pip//pyyaml"],
)
