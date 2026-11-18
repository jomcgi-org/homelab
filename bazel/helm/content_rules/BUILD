# Hand-managed targets (same convention as bazel/tools/format/readme_structure).
# gazelle:exclude content_rules.py
# gazelle:exclude content_rules_test.py
# gazelle:exclude coverage_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The application charts on the factory cluster: each ../<project>/deploy
# entry in projects/platform-factory/kustomization.yaml except the factory.
# Each sets content_rules = True on its argocd_app, which creates the marker.
APPLICATIONS = [
    "factory-explore",
    "factory-landing",
    "factory-public",
    "jomcgi-site",
]

py_library(
    name = "content_rules_lib",
    srcs = ["content_rules.py"],
    imports = ["."],
    deps = ["@pip//pyyaml"],
)

py_venv_binary(
    name = "content_rules",
    srcs = ["content_rules.py"],  # keep
    main = "content_rules.py",
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pyyaml"],  # keep
)

py_test(
    name = "content_rules_test",
    srcs = ["content_rules_test.py"],
    deps = [
        ":content_rules_lib",
        "@pip//pytest",
    ],
)

py_test(
    name = "coverage_test",
    srcs = ["coverage_test.py"],
    data = [
        "//projects:factory_kustomization",
        "//projects/jomcgi-site/deploy:content_rules_opt_in_prod",
    ] + ["//projects/{}/deploy:content_rules_opt_in".format(a) for a in APPLICATIONS],
    env = {
        "APPLICATIONS": " ".join(APPLICATIONS),
        "KUSTOMIZATION": "$(rootpath //projects:factory_kustomization)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
