"""Every application chart on the factory cluster opts in to the content rules.

The BUILD file lists the applications, and Bazel fails the build when one of
them has no content_rules_opt_in marker. This test fails when the factory
cluster's kustomization deploys an application that the list omits.
"""

import os

import yaml

# The factory chart is the platform that runs agents, not an application.
NOT_APPLICATIONS = {"factory"}


def applications(kustomization):
    out = set()
    for entry in kustomization.get("resources") or []:
        parts = entry.split("/")
        if len(parts) == 3 and parts[0] == ".." and parts[2] == "deploy":
            out.add(parts[1])
    return out - NOT_APPLICATIONS


def test_applications_parses_deploy_entries():
    k = {
        "resources": [
            "./cilium",
            "../factory/deploy",
            "../site/deploy",
            "../site/chart",
        ]
    }
    assert applications(k) == {"site"}


def test_every_application_opts_in():
    path = os.environ["KUSTOMIZATION"]
    listed = set(os.environ["APPLICATIONS"].split())
    with open(path) as f:
        deployed = applications(yaml.safe_load(f))
    missing = deployed - listed
    assert not missing, (
        f"{sorted(missing)} deploy on the factory cluster without the content rules: "
        "set content_rules = True on its argocd_app and add it to APPLICATIONS in "
        "bazel/helm/content_rules/BUILD"
    )
    stale = listed - deployed
    assert not stale, f"{sorted(stale)} are listed but not deployed; remove them"
