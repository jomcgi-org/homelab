# gazelle:ignore
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The actual OCI image must run; local engine failures are test failures.
py_test(
    name = "publisher_image_smoke_test",
    timeout = "long",
    srcs = ["publisher_image_test.py"],
    args = ["-s"],  # Preserve image ID/command/import proof in test.log.
    data = [
        "//projects/monolith:public_assets_backend_image",
        "//projects/monolith:public_assets_backend_loader",
        "//projects/monolith-public/chart:chart_render_inputs",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "PUBLIC_BACKEND_IMAGE": "$(rootpath //projects/monolith:public_assets_backend_image)",
        "PUBLIC_BACKEND_LOADER": "$(rootpath //projects/monolith:public_assets_backend_loader)",
    },
    tags = [
        "no-remote-exec",
        "no-sandbox",
    ],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
