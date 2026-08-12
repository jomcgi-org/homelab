"""The dev deployment must not claim any identity production owns.

Dev renders this SAME chart with an overlay layered on production's values, so
everything not explicitly overridden is inherited. That is the point, and it is
also the hazard: inheriting is safe for settings describing how a workload
BEHAVES, and unsafe for the ones describing WHO IT CLAIMS TO BE.

Two of those slipped through in one evening, both silently:

  1. Dev inherited cfIngress and claimed private.jomcgi.dev and
     ships.jomcgi.dev. Every HTTPRoute attaches to the one cloudflare-ingress
     Gateway, and Gateway API MERGES routes attaching to the same Gateway for
     the same hostname, so production traffic could have been served by a dev
     build running against a copy of production's data. Nothing errors.

  2. Dev ran under releaseName `monolith`, so it rendered ClusterRole/monolith,
     which is cluster-scoped and therefore the very object production owns.
     ArgoCD's shared-resource protection caught that one:

       ClusterRole/monolith is part of applications argocd/monolith-dev
       and monolith

Neither is reachable by a normal unit test, and neither shows up as a red
render: both charts template perfectly. Only comparing the two rendered
outputs against each other finds them, which is what this does.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# Kinds with no namespace: two Applications rendering the same name means one
# object with two owners, not two objects.
CLUSTER_SCOPED = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "PriorityClass",
    "StorageClass",
    "ValidatingWebhookConfiguration",
    "MutatingWebhookConfiguration",
}

_DOC_KIND = re.compile(r"^kind:\s*(\S+)\s*$", re.M)
_DOC_NAME = re.compile(r"^\s{2}name:\s*(\S+)\s*$", re.M)
_HOSTNAME = re.compile(r"^\s*-\s*[\"']?([a-z0-9.-]+\.jomcgi\.dev)[\"']?\s*$", re.M)


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "Chart.yaml").exists():
        return here
    raise RuntimeError("Could not find chart Chart.yaml")


def _render(release: str, values: list[Path]) -> str:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [helm_bin, "template", release, str(_chart_dir()), "--namespace", release]
    for v in values:
        argv += ["--values", str(v)]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _docs(rendered: str):
    for doc in rendered.split("\n---"):
        kind = _DOC_KIND.search(doc)
        name = _DOC_NAME.search(doc)
        if kind and name:
            yield kind.group(1), name.group(1), doc


_RELEASE_NAME = re.compile(r"^\s*releaseName:\s*(\S+)\s*$", re.M)


def _release_name(application_yaml: Path) -> str:
    """Read releaseName from the Application, NOT from this test.

    Load-bearing. The defect this file exists to catch lived in
    deploy/application.yaml, not in the chart: the chart renders disjoint names
    perfectly well when handed two different release names. Hardcoding them here
    would produce a test that passes while the Application says otherwise, which
    is the exact shape of a guard that cannot fail.
    """
    match = _RELEASE_NAME.search(application_yaml.read_text())
    assert match, f"no releaseName found in {application_yaml}"
    return match.group(1)


@pytest.fixture(scope="module")
def renders():
    chart = _chart_dir()
    base = [chart / "values.yaml", Path(os.environ["DEPLOY_VALUES"])]
    dev_overlay = Path(os.environ["DEV_VALUES"])
    prod_release = _release_name(Path(os.environ["PROD_APPLICATION"]))
    dev_release = _release_name(Path(os.environ["DEV_APPLICATION"]))
    return {
        "prod": _render(prod_release, base),
        "dev": _render(dev_release, base + [dev_overlay]),
    }


def test_dev_claims_no_cluster_scoped_object_production_owns(renders):
    """The releaseName collision. Cluster-scoped names must be disjoint."""
    prod = {f"{k}/{n}" for k, n, _ in _docs(renders["prod"]) if k in CLUSTER_SCOPED}
    dev = {f"{k}/{n}" for k, n, _ in _docs(renders["dev"]) if k in CLUSTER_SCOPED}
    shared = prod & dev
    assert not shared, (
        f"dev and production both render {sorted(shared)}. These kinds have no "
        "namespace, so that is ONE object with two ArgoCD owners, both with "
        "selfHeal. Give the dev Application a distinct releaseName."
    )
    # Guard the guard: if production stops rendering cluster-scoped objects
    # entirely, the disjointness above passes for the wrong reason.
    assert prod, "production rendered no cluster-scoped objects; this test is inert"


def test_dev_claims_no_hostname(renders):
    """The ingress collision. Dev must claim nothing on the shared Gateway."""
    dev_hosts = set()
    for kind, _name, doc in _docs(renders["dev"]):
        if kind == "HTTPRoute":
            dev_hosts.update(_HOSTNAME.findall(doc))
    assert not dev_hosts, (
        f"dev renders HTTPRoutes claiming {sorted(dev_hosts)}. Every route "
        "attaches to the one cloudflare-ingress Gateway, and Gateway API merges "
        "routes claiming the same hostname, so production traffic could be "
        "served by dev. Dev needs a hostname of its own before ingress is "
        "enabled there."
    )
    # Guard the guard, as above: production must actually claim hostnames, or
    # an empty dev set proves nothing about the mechanism.
    prod_hosts = set()
    for kind, _name, doc in _docs(renders["prod"]):
        if kind == "HTTPRoute":
            prod_hosts.update(_HOSTNAME.findall(doc))
    assert prod_hosts, "production claimed no hostnames; this test is inert"


def test_dev_does_not_render_the_refresh_workflow(renders):
    """Production owns the refresh.

    It renders into jobs.workflowNamespace rather than the release namespace, so
    two deployments rendering it would put two identically-named CronWorkflows
    into monolith-workflows and the Applications would fight every sync.
    """
    assert "cnpg-dev-refresh" in renders["prod"]
    assert "cnpg-dev-refresh" not in renders["dev"]


def test_dev_mutes_leader_singletons_and_production_does_not(renders):
    """The side-effect mute, asserted in BOTH directions.

    Asserting only that dev is false would pass if the value stopped rendering
    at all, which is the failure mode `| default true` would have produced.
    """
    assert (
        'name: MONOLITH_LEADER_SINGLETONS\n              value: "false"'
        in renders["dev"]
    )
    assert (
        'name: MONOLITH_LEADER_SINGLETONS\n              value: "true"'
        in renders["prod"]
    )
