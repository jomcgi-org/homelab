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
import tempfile
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


def _write_forced_jobs_image() -> Path:
    """jobs.image is injected at CHART BUILD TIME by helm_images_values.

    A plain `helm template` therefore leaves it empty, cronworkflows.yaml is
    gated on it, and NOTHING renders. That is not a harmless gap: a
    `grep -c CronWorkflow` on an unforced render returns 0 for production too,
    so an earlier check compared 0 against 0, reported the environments
    identical, and missed dev owning production's scheduled jobs in the live
    cluster.
    """
    path = Path(tempfile.gettempdir()) / "monolith_forced_jobs_image.yaml"
    path.write_text(
        "jobs:\n"
        "  image:\n"
        "    repository: registry.invalid/forced-for-test\n"
        "    tag: test\n"
    )
    return path


_FORCED_JOBS_IMAGE = _write_forced_jobs_image()


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
    # jobs.image is injected at CHART BUILD TIME by helm_images_values, so a
    # plain `helm template` leaves it empty and cronworkflows.yaml, gated on it,
    # renders NOTHING. Forcing a value here is what makes the CronWorkflows
    # visible to these comparisons at all.
    #
    # This is not a detail. A `grep -c CronWorkflow` on an unforced render
    # returns 0 for production too, so an earlier version of this check compared
    # 0 against 0 and reported the environments identical while dev was live in
    # the cluster owning production's scheduled jobs.
    # Forced through a values FILE inserted before the caller's, never --set.
    #
    # Two reasons, both learned by getting it wrong. jobs.image is a MAP (the
    # template reads .repository), so `--set jobs.image=<str>` dies on the field
    # access. And `--set` is applied LAST, so it would override dev's
    # `jobs.image: ""` and make dev render the CronWorkflows it exists to
    # suppress, turning a real assertion into a false failure.
    #
    # Ordering before the caller's files means production picks the forced image
    # up (it has none locally) while dev's empty string still wins. Delete dev's
    # override and dev inherits this instead, which is precisely the collision
    # these comparisons must catch.
    argv = argv[:4] + ["--values", str(_FORCED_JOBS_IMAGE)] + argv[4:]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _pinned_namespace(doc: str) -> str | None:
    """The metadata.namespace a doc pins itself to, if any.

    A resource that names its own namespace escapes the release namespace, so
    the Application's destination does NOT separate it from production's copy.
    That is the whole hazard class this file guards.
    """
    match = re.search(r"^\s{2}namespace:\s*(\S+)\s*$", doc, re.M)
    return match.group(1) if match else None


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


def test_dev_claims_no_resource_pinned_outside_its_namespace(renders):
    """The general form of the collision, and the one that actually bit.

    Two earlier cases were special cases of this: cluster-scoped RBAC (no
    namespace at all) and HTTPRoutes (namespaced, but attached to a Gateway
    that merges by hostname). The third was CronWorkflows, which pin
    metadata.namespace to jobs.workflowNamespace and are named `{{ .name }}`
    with no release prefix, so dev and production render byte-identical
    identities into monolith-workflows.

    That one reached the cluster: campsites-refresh came back labelled
    app.kubernetes.io/instance: monolith-dev, and its DATABASE_URL is the
    Kyverno-cloned PRODUCTION credential.

    An Application's destination namespace separates only the resources that
    accept it. Anything naming its own namespace opts out of that separation.
    """

    def pinned(env):
        out = set()
        for kind, name, doc in _docs(renders[env]):
            ns = _pinned_namespace(doc)
            if ns:
                out.add(f"{ns}/{kind}/{name}")
        return out

    shared = pinned("prod") & pinned("dev")
    assert not shared, (
        f"dev and production both render {sorted(shared)}. These pin their own "
        "metadata.namespace, so the Application's destination does not separate "
        "them: that is one object with two owners. Disable it in dev's overlay "
        "or give it a release-scoped name."
    )
    assert pinned("prod"), (
        "production pinned nothing outside its namespace; this test is inert. "
        "If cronworkflows.yaml stopped rendering, check jobs.image is forced."
    )


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
