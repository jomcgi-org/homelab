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
import yaml

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


_DESTINATION = re.compile(r"^\s*destinationPath:\s*(\S+)\s*$", re.M)


def _write_forced_backup() -> Path:
    """Values that force backups on, to prove the collision is real.

    Used only by the positive control in
    test_dev_does_not_write_to_productions_backup_store.
    """
    path = Path(tempfile.gettempdir()) / "monolith_forced_backup.yaml"
    path.write_text("postgres:\n  backup:\n    enabled: true\n")
    return path


_FORCED_BACKUP = _write_forced_backup()


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
    gke_overlay = Path(os.environ["GKE_VALUES"])
    prod_release = _release_name(Path(os.environ["PROD_APPLICATION"]))
    dev_release = _release_name(Path(os.environ["DEV_APPLICATION"]))
    return {
        "prod": _render(prod_release, base),
        "dev": _render(dev_release, base + [dev_overlay]),
        "gke": _render(prod_release, base + [gke_overlay]),
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


def test_dev_claims_no_hostname_production_claims(renders):
    """The ingress collision, generalised now that dev has a hostname.

    This began as "dev claims NO hostname", which was right while dev had none
    of its own. Once dev.jomcgi.dev exists that phrasing would have to be
    deleted to let dev work, and deleting it would silently remove the check
    that caught dev claiming private.jomcgi.dev and ships.jomcgi.dev.

    So it asserts the property that was always the real one: the environments
    claim DISJOINT hostnames. Every HTTPRoute attaches to the one
    cloudflare-ingress Gateway, and Gateway API merges routes claiming the same
    hostname, so an overlap is production traffic reaching a dev build.
    """

    def hosts(env):
        out = set()
        for kind, _name, doc in _docs(renders[env]):
            if kind == "HTTPRoute":
                out.update(_HOSTNAME.findall(doc))
        return out

    shared = hosts("prod") & hosts("dev")
    assert not shared, (
        f"dev and production both claim {sorted(shared)}. Routes attaching to "
        "one Gateway for one hostname are MERGED, so production traffic could "
        "be served by a dev build running against a copy of production's data."
    )
    # Both halves must be non-empty or disjointness is vacuous: if dev stopped
    # rendering ingress, or production did, this would pass while proving
    # nothing about the mechanism.
    assert hosts("prod"), "production claimed no hostname; this test is inert"
    assert hosts("dev"), (
        "dev claimed no hostname; this test is inert. If dev ingress was "
        "deliberately disabled, assert that explicitly rather than leaving "
        "this passing on an empty set."
    )


def test_dev_exposes_no_unauthenticated_path(renders):
    """Everything on dev's hostname sits behind authentik.

    The github-webhook route carries NO SecurityPolicy by design: GitHub
    bypasses Cloudflare Access at the edge and sends no JWT, so a policy there
    would reject every real delivery, and HMAC in the handler is its gate.

    Correct for production, a hole anywhere else: an unauthenticated route on a
    host that is otherwise gated, which GitHub is not even delivering to.
    """
    dev_routes = {
        name for kind, name, _ in _docs(renders["dev"]) if kind == "HTTPRoute"
    }
    assert not any("github-webhook" in n for n in dev_routes), (
        f"dev renders an ungated webhook route: {sorted(dev_routes)}. That "
        "route intentionally has no SecurityPolicy, so on dev's hostname it is "
        "an open endpoint."
    )
    prod_routes = {
        name for kind, name, _ in _docs(renders["prod"]) if kind == "HTTPRoute"
    }
    assert any("github-webhook" in n for n in prod_routes), (
        "production stopped rendering the webhook route; this test is inert "
        "and GitHub deliveries are probably broken."
    )


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


def test_production_jobs_receive_otel_endpoint(renders):
    workflows = [
        yaml.safe_load(doc)
        for kind, _name, doc in _docs(renders["prod"])
        if kind == "CronWorkflow"
    ]
    job_workflows = [
        workflow
        for workflow in workflows
        if workflow["spec"]["workflowSpec"]
        .get("podMetadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/part-of")
        == "monolith-jobs"
    ]
    assert job_workflows, "production rendered no jobs CronWorkflows; test is inert"
    for workflow in job_workflows:
        container = workflow["spec"]["workflowSpec"]["templates"][0]["container"]
        env = {item["name"]: item.get("value") for item in container["env"]}
        endpoint = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
        assert endpoint.endswith(":4318/v1/traces"), (
            f"{workflow['metadata']['name']} has no usable OTLP/HTTP endpoint"
        )


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


def test_refresh_job_does_not_run_the_extension_image(renders):
    """The refresh job needs a RUNTIME image, not an extension artifact.

    This shipped pointing at postgres.pgvector.image, which is the declarative
    EXTENSION image for `vector` (cnpg-cluster.yaml's extensions block): a
    minimal artifact carrying extension binaries and no shell. The workflow
    could never start:

        failed to find name in PATH: exec: "/bin/sh": no such file or directory

    It failed only when submitted by hand. On its own schedule it would have
    failed at 04:00 with an exit code nobody was watching, leaving dev with an
    empty database indefinitely.

    Helm cannot check whether an image contains a shell, so this pins the thing
    it CAN check: that the job and the extension are not the same image.
    """
    import re as _re

    ext = _re.search(r"^\s+reference:\s*(\S+)\s*$", renders["prod"], _re.M)
    assert ext, "no declarative extension image rendered; this test is inert"

    job_images = _re.findall(
        r"^\s+image:\s*[\"']?(\S+?)[\"']?\s*$",
        "\n".join(
            doc
            for kind, _n, doc in _docs(renders["prod"])
            if kind == "CronWorkflow" and "cnpg-dev-refresh" in doc
        ),
        _re.M,
    )
    assert job_images, "the refresh CronWorkflow did not render; this test is inert"
    assert ext.group(1) not in job_images, (
        f"the refresh job runs {ext.group(1)}, the declarative extension image. "
        "That artifact has no shell and the workflow cannot start. Use a "
        "postgres RUNTIME image carrying pg_dump, pg_restore and psql."
    )


def test_dev_takes_no_backups(renders):
    """Dev must take no backups, and its silence must be dev's doing.

    This shipped asserting something stronger and wrong: that the two
    environments must never share a `destinationPath`, because a shared path
    meant a shared barman catalogue and dev's 14 day retentionPolicy would
    prune production's base backups and WALs.

    The CRD refutes it in one line:

        serverName: The server name in object storage, the cluster name is
                    used if this parameter is omitted

    barman namespaces each cluster under the destination by serverName, so
    production writes to <destinationPath>/monolith-pg/ and dev would have
    written to <destinationPath>/monolith-dev-pg/. Separate catalogues,
    separate retention, no cross-pruning. The GCS layout is
    gs://h0melab-cnpg-backups/monolith-pg/monolith-pg/wals/, with the doubled segment
    coming from the defaulted serverName.

    What remains is smaller and still worth a guard. Dev is reseeded from
    production nightly, so backing dev up stores a SECOND copy of production's
    rows: roughly 1.5 GB of base backup a day, held 14 days, using the same
    production GCS service account. That is waste, and the same rows are
    exposed twice.

    So the invariant is just that dev takes no backups, plus a control proving
    that is because of dev's override and not because the chart's barman
    templates went away.
    """

    def destinations(env):
        return {m.strip("\"'") for m in _DESTINATION.findall(renders[env])}

    dev_dest = destinations("dev")
    assert not dev_dest, (
        f"dev renders a backup object store ({sorted(dev_dest)}). Dev is a "
        "nightly copy of production, so this stores production's rows a second "
        "time using the production GCS service account. Keep "
        "postgres.backup.enabled false in dev's overlay."
    )

    prod_dest = destinations("prod")
    assert prod_dest, (
        "production renders no backup destinationPath, so it has no backups at "
        "all and dev's emptiness above proves nothing. That was precisely the "
        "state #4714 was filed about: the whole barman block sat behind "
        "`enabled: false` and every render was empty."
    )

    # Control. Dev is quiet because its overlay says so, not because the chart
    # lost its backup templates. Without this, deleting the barman block from
    # the chart entirely would leave this test green.
    chart = _chart_dir()
    forced_dev = _render(
        _release_name(Path(os.environ["DEV_APPLICATION"])),
        [
            chart / "values.yaml",
            Path(os.environ["DEPLOY_VALUES"]),
            Path(os.environ["DEV_VALUES"]),
            _FORCED_BACKUP,
        ],
    )
    assert _DESTINATION.findall(forced_dev), (
        "forcing postgres.backup.enabled=true renders dev no object store, so "
        "dev's override is not what is keeping it quiet and the assertion "
        "above passes for the wrong reason. Check the chart still has the "
        "barman templates."
    )


def test_gke_recovery_has_app_credentials_and_a_distinct_archive(renders):
    """Recovery must mint usable app credentials and archive under a new name."""
    cluster = next(
        doc
        for doc in yaml.safe_load_all(renders["gke"])
        if isinstance(doc, dict)
        and doc.get("kind") == "Cluster"
        and doc.get("metadata", {}).get("name") == "monolith-pg"
    )

    recovery = cluster["spec"]["bootstrap"]["recovery"]
    assert recovery["database"] == "monolith"
    assert recovery["owner"] == "app"

    # An absent backup section is the SAFE degenerate case: nothing archives,
    # so nothing can collide with the home archive. The GKE overlay holds
    # backup disabled while the gke-apps chart pin predates the serverName
    # template (an enabled backup on the pinned chart archives straight into
    # the home prefix). Once backup is enabled again, its archive name must
    # differ from the recovery source.
    if "backup" not in cluster["spec"]:
        return
    backup_store = cluster["spec"]["backup"]["barmanObjectStore"]
    backup_server = backup_store.get("serverName", cluster["metadata"]["name"])
    recovery_store = cluster["spec"]["externalClusters"][0]
    recovery_server = recovery_store["barmanObjectStore"].get(
        "serverName", recovery_store["name"]
    )
    assert backup_server != recovery_server, (
        f"GKE backup and recovery both resolve to serverName {backup_server!r}. "
        "CNPG requires the recovery source to remain on the home archive while "
        "the recovered cluster writes to a distinct archive."
    )


# Snapshot of the default grants before optional RBAC gates. Comparing complete
# rule/binding projections catches a differently named binding into production.
_DEFAULT_CLUSTER_RULES = [
    {
        "apiGroups": [""],
        "resources": [
            "nodes",
            "pods",
            "services",
            "configmaps",
            "events",
            "namespaces",
        ],
        "verbs": ["get", "list"],
    },
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["nodes/proxy"], "verbs": ["get"]},
    {
        "apiGroups": ["apps"],
        "resources": ["deployments", "statefulsets", "daemonsets", "replicasets"],
        "verbs": ["get", "list"],
    },
    {
        "apiGroups": ["argoproj.io"],
        "resources": ["applications"],
        "verbs": ["get", "list", "patch"],
    },
    {
        "apiGroups": ["kargo.akuity.io"],
        "resources": ["freights"],
        "verbs": ["get", "list"],
    },
    {
        "apiGroups": ["metrics.k8s.io"],
        "resources": ["nodes", "pods"],
        "verbs": ["get", "list"],
    },
]
_DEFAULT_SCHEDULER_RULES = [
    {"apiGroups": ["argoproj.io"], "resources": ["cronworkflows"], "verbs": ["list"]},
    {"apiGroups": ["argoproj.io"], "resources": ["workflows"], "verbs": ["create"]},
]
_DEFAULT_FAAS_RULES = [
    {
        "apiGroups": ["embervm.dev"],
        "resources": ["workloads"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {"apiGroups": ["embervm.dev"], "resources": ["workloads/status"], "verbs": ["get"]},
]
_RBAC_KINDS = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}


def _rbac_objects(rendered):
    return {
        (doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"]): {
            key: doc[key] for key in ("rules", "roleRef", "subjects") if key in doc
        }
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict) and doc.get("kind") in _RBAC_KINDS
    }


def _expected_rbac(release, enabled):
    subject = {"kind": "ServiceAccount", "name": release, "namespace": release}
    expected = {}
    for gate, kind, namespace, name, rules, subjects in [
        (
            "clusterAccess",
            "ClusterRole",
            None,
            release,
            _DEFAULT_CLUSTER_RULES,
            [subject],
        ),
        (
            "schedulerWorkflows",
            "Role",
            "monolith-workflows",
            f"{release}-scheduler-workflows",
            _DEFAULT_SCHEDULER_RULES,
            [subject],
        ),
        (
            "faas",
            "Role",
            "embervm",
            f"{release}-faas",
            _DEFAULT_FAAS_RULES,
            [
                subject,
                {
                    "kind": "ServiceAccount",
                    "name": "monolith-stats",
                    "namespace": "monolith-workflows",
                },
            ],
        ),
    ]:
        if not enabled[gate]:
            continue
        expected[kind, namespace, name] = {"rules": rules}
        expected[f"{kind}Binding", namespace, name] = {
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": kind,
                "name": name,
            },
            "subjects": subjects,
        }
    return expected


@pytest.mark.parametrize("environment", ["defaults", "prod", "gke"])
def test_default_rbac_preserves_rule_and_binding_contract(environment, renders):
    release = _release_name(Path(os.environ["PROD_APPLICATION"]))
    rendered = (
        _render(release, []) if environment == "defaults" else renders[environment]
    )
    assert _rbac_objects(rendered) == _expected_rbac(
        release, {"clusterAccess": True, "schedulerWorkflows": True, "faas": True}
    )


@pytest.mark.parametrize("cluster", [False, True])
@pytest.mark.parametrize("scheduler", [False, True])
@pytest.mark.parametrize("faas", [False, True])
def test_optional_rbac_gates_remove_both_roles_and_all_subject_bindings(
    tmp_path, cluster, scheduler, faas
):
    # Layer onto actual home values, where the accidental production grants
    # matter, while keeping a distinct release identity.
    enabled = {"clusterAccess": cluster, "schedulerWorkflows": scheduler, "faas": faas}
    override = tmp_path / "rbac.yaml"
    override.write_text(
        yaml.safe_dump(
            {"rbac": {key: {"enabled": value} for key, value in enabled.items()}}
        )
    )
    actual = _rbac_objects(
        _render("recovery", [Path(os.environ["DEPLOY_VALUES"]), override])
    )
    assert actual == _expected_rbac("recovery", enabled)
    if not any(enabled.values()):
        # No additive alternate binding may retain access for either the
        # backend or the cross-namespace monolith-stats workflow identity.
        assert actual == {}


@pytest.mark.parametrize("gate", ["clusterAccess", "schedulerWorkflows", "faas"])
@pytest.mark.parametrize("invalid", ["false", 1])
def test_rbac_gate_rejects_non_boolean_values(tmp_path, gate, invalid):
    override = tmp_path / "invalid-rbac.yaml"
    override.write_text(yaml.safe_dump({"rbac": {gate: {"enabled": invalid}}}))
    with pytest.raises(RuntimeError, match=rf"rbac\.{gate}\.enabled must be a boolean"):
        _render("recovery", [override])


# Existing SQL role identities and defaults, independent of the values under
# test. Atlas grants require these roles even when no client may log in.
_CLIENT_ROLE_SPECS = [
    (
        "publicReader",
        "public_reader",
        "monolith-pg-public-reader",
        "Read-only public surface (ADR 004); GRANTs in Atlas migration",
    ),
    (
        "agentsWriter",
        "agents_writer",
        "monolith-pg-agents-writer",
        "monolith-agents tier (#5656); GRANTs in Atlas migration",
    ),
    (
        "publicWriter",
        "public_writer",
        "monolith-pg-public-writer",
        "Public-tier write role (ADR 005); GRANTs in Atlas migration",
    ),
]
_CLIENT_ROLE_KEYS = [entry[0] for entry in _CLIENT_ROLE_SPECS]


def _expected_cnpg_roles(environment):
    clients = [
        {
            "name": name,
            "ensure": "present",
            "login": True,
            "passwordSecret": {"name": secret},
            "comment": comment,
        }
        for _key, name, secret, comment in _CLIENT_ROLE_SPECS
    ]
    optional = []
    if environment in {"prod", "gke"}:
        optional.append(
            {
                "name": "embervm",
                "ensure": "present",
                "login": True,
                "passwordSecret": {"name": "monolith-pg-embervm-oplog"},
                "comment": "EmberVM op-log owner (ADR embervm/007); owns only its own database",
            }
        )
    if environment == "gke":
        optional.append(
            {
                "name": "spire",
                "ensure": "present",
                "login": True,
                "passwordSecret": {"name": "monolith-pg-spire"},
                "comment": "SPIRE server datastore owner (ADR embervm/041); owns only its own database",
            }
        )
    if environment == "prod":
        optional.append(
            {
                "name": "dump_reader",
                "ensure": "present",
                "login": True,
                "passwordSecret": {"name": "monolith-pg-dump-reader"},
                "inRoles": ["pg_read_all_data"],
                "comment": "Dev refresh dump reader",
            }
        )
    return [*clients[:2], *optional, clients[2]]


def _cnpg_cluster(documents):
    clusters = [
        doc
        for doc in documents
        if isinstance(doc, dict)
        and doc.get("kind") == "Cluster"
        and doc.get("apiVersion") == "postgresql.cnpg.io/v1"
    ]
    assert len(clusters) == 1, "expected exactly one CNPG Cluster"
    return clusters[0]


def _render_cnpg_override(tmp_path, override, *, production=False):
    values = tmp_path / "cnpg-values.yaml"
    values.write_text(yaml.safe_dump(override))
    base = [Path(os.environ["DEPLOY_VALUES"])] if production else []
    return [
        doc
        for doc in yaml.safe_load_all(_render("monolith-dev", [*base, values]))
        if isinstance(doc, dict)
    ]


@pytest.mark.parametrize("environment", ["defaults", "prod", "gke"])
def test_cnpg_default_managed_roles_preserve_complete_contract(environment, renders):
    rendered = (
        _render("monolith", []) if environment == "defaults" else renders[environment]
    )
    cluster = _cnpg_cluster(yaml.safe_load_all(rendered))
    assert cluster["metadata"]["name"] == "monolith-pg"
    assert cluster["spec"]["managed"]["roles"] == _expected_cnpg_roles(environment)


@pytest.mark.parametrize("public_reader", [False, True])
@pytest.mark.parametrize("agents_writer", [False, True])
@pytest.mark.parametrize("public_writer", [False, True])
def test_cnpg_client_login_controls_preserve_roles_and_optional_owners(
    tmp_path, public_reader, agents_writer, public_writer
):
    settings = dict(
        zip(_CLIENT_ROLE_KEYS, (public_reader, agents_writer, public_writer))
    )
    documents = _render_cnpg_override(
        tmp_path,
        {
            "postgres": {
                "clientRoles": {
                    key: {"login": login} for key, login in settings.items()
                }
            }
        },
        production=True,
    )
    expected = _expected_cnpg_roles("prod")
    by_name = {role["name"]: role for role in expected}
    for key, name, _secret, _comment in _CLIENT_ROLE_SPECS:
        role = by_name[name]
        role["login"] = settings[key]
        if not settings[key]:
            del role["passwordSecret"]
    assert _cnpg_cluster(documents)["spec"]["managed"]["roles"] == expected


def test_cnpg_enabled_roles_use_their_configured_secret_names(tmp_path):
    settings = {
        key: {"passwordSecret": f"recovery-{key.lower()}.credential"}
        for key in _CLIENT_ROLE_KEYS
    }
    documents = _render_cnpg_override(tmp_path, {"postgres": {"clientRoles": settings}})
    expected = _expected_cnpg_roles("defaults")
    for role, key in zip(expected, _CLIENT_ROLE_KEYS):
        role["passwordSecret"] = {"name": settings[key]["passwordSecret"]}
    assert _cnpg_cluster(documents)["spec"]["managed"]["roles"] == expected


@pytest.mark.parametrize("secret", ["", None])
def test_cnpg_nologin_roles_allow_cleared_secret_references(tmp_path, secret):
    documents = _render_cnpg_override(
        tmp_path,
        {
            "postgres": {
                "clientRoles": {
                    key: {"login": False, "passwordSecret": secret}
                    for key in _CLIENT_ROLE_KEYS
                }
            }
        },
    )
    expected = _expected_cnpg_roles("defaults")
    for role in expected:
        role["login"] = False
        del role["passwordSecret"]
    assert _cnpg_cluster(documents)["spec"]["managed"]["roles"] == expected


def test_cnpg_fresh_dev_database_and_consumers_have_isolated_references(tmp_path):
    documents = _render_cnpg_override(
        tmp_path,
        {
            "jobs": {"image": ""},
            "postgres": {
                "instances": 1,
                "storage": {"size": "10Gi", "storageClass": "standard-rwo"},
                "clientRoles": {key: {"login": False} for key in _CLIENT_ROLE_KEYS},
                "bootstrap": {"recovery": None},
                "externalClusters": [],
                "backup": {"enabled": False},
                "devRefresh": {"enabled": False},
                "spire": {"enabled": False},
                "embervmOpLog": {
                    "enabled": True,
                    "database": "embervm_oplog_recovery",
                    "role": "embervm_recovery",
                    "passwordSecret": "monolith-dev-pg-embervm-oplog",
                },
            },
        },
    )
    cluster = _cnpg_cluster(documents)
    assert cluster["metadata"]["name"] == "monolith-dev-pg"
    spec = cluster["spec"]
    assert spec["instances"] == 1
    assert spec["storage"] == {"size": "10Gi", "storageClass": "standard-rwo"}
    assert spec["bootstrap"] == {
        "initdb": {
            "database": "monolith",
            "owner": "app",
            "postInitSQL": ["CREATE EXTENSION IF NOT EXISTS vector"],
        }
    }
    assert "externalClusters" not in spec
    assert "backup" not in spec
    expected = _expected_cnpg_roles("defaults")
    for role in expected:
        role["login"] = False
        del role["passwordSecret"]
    expected.insert(
        2,
        {
            "name": "embervm_recovery",
            "ensure": "present",
            "login": True,
            "passwordSecret": {"name": "monolith-dev-pg-embervm-oplog"},
            "comment": "EmberVM op-log owner (ADR embervm/007); owns only its own database",
        },
    )
    assert spec["managed"]["roles"] == expected
    assert not any(
        doc.get("kind") in {"ScheduledBackup", "CronWorkflow"} for doc in documents
    )
    databases = [doc for doc in documents if doc.get("kind") == "Database"]
    assert len(databases) == 1
    assert databases[0]["metadata"]["name"] == "monolith-dev-pg-embervm-oplog"
    assert databases[0]["spec"] == {
        "cluster": {"name": "monolith-dev-pg"},
        "name": "embervm_oplog_recovery",
        "owner": "embervm_recovery",
        "ensure": "present",
        "databaseReclaimPolicy": "retain",
    }
    deployment = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "monolith-dev"
    )
    containers = {
        container["name"]: container
        for container in deployment["spec"]["template"]["spec"]["containers"]
    }
    expected_ref = {"name": "monolith-dev-pg-app", "key": "uri"}
    for name in ("backend", "progress-ingest"):
        env = {entry["name"]: entry for entry in containers[name]["env"]}
        assert env["DATABASE_URL"] == {
            "name": "DATABASE_URL",
            "valueFrom": {"secretKeyRef": expected_ref},
        }
    migrations = [doc for doc in documents if doc.get("kind") == "AtlasMigration"]
    assert len(migrations) == 1
    assert migrations[0]["metadata"]["name"] == "monolith-dev"
    assert migrations[0]["spec"]["urlFrom"] == {"secretKeyRef": expected_ref}


@pytest.mark.parametrize("key", _CLIENT_ROLE_KEYS)
@pytest.mark.parametrize("invalid", ["false", 0, None])
def test_cnpg_rejects_non_boolean_client_login(tmp_path, key, invalid):
    with pytest.raises(
        RuntimeError,
        match=re.escape(f"postgres.clientRoles.{key}.login must be a boolean"),
    ):
        _render_cnpg_override(
            tmp_path, {"postgres": {"clientRoles": {key: {"login": invalid}}}}
        )


@pytest.mark.parametrize("key", _CLIENT_ROLE_KEYS)
@pytest.mark.parametrize(
    "invalid",
    [None, "", " ", 1, [], "*", "UPPER", "fixture/other", "fixture..other", "a" * 254],
)
def test_cnpg_rejects_invalid_secret_for_client_login(tmp_path, key, invalid):
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            f"postgres.clientRoles.{key}.passwordSecret must be a valid Secret name when login is true"
        ),
    ):
        _render_cnpg_override(
            tmp_path, {"postgres": {"clientRoles": {key: {"passwordSecret": invalid}}}}
        )


@pytest.mark.parametrize("invalid", [None, "roles"])
def test_cnpg_rejects_invalid_client_roles_map(tmp_path, invalid):
    with pytest.raises(RuntimeError, match="postgres.clientRoles must be a map"):
        _render_cnpg_override(tmp_path, {"postgres": {"clientRoles": invalid}})


@pytest.mark.parametrize("key", _CLIENT_ROLE_KEYS)
def test_cnpg_rejects_missing_client_role_map(tmp_path, key):
    with pytest.raises(
        RuntimeError, match=re.escape(f"postgres.clientRoles.{key} must be a map")
    ):
        _render_cnpg_override(tmp_path, {"postgres": {"clientRoles": {key: None}}})


@pytest.mark.parametrize("key", _CLIENT_ROLE_KEYS)
@pytest.mark.parametrize("secret", ["123", "true", "null", "on"])
def test_cnpg_yaml_looking_secret_names_remain_exact_strings(tmp_path, key, secret):
    documents = _render_cnpg_override(
        tmp_path, {"postgres": {"clientRoles": {key: {"passwordSecret": secret}}}}
    )
    expected = _expected_cnpg_roles("defaults")
    role_name = next(
        name
        for role_key, name, _secret, _comment in _CLIENT_ROLE_SPECS
        if role_key == key
    )
    for role in expected:
        if role["name"] == role_name:
            role["passwordSecret"] = {"name": secret}
    roles = _cnpg_cluster(documents)["spec"]["managed"]["roles"]
    assert roles == expected
    actual_name = next(role for role in roles if role["name"] == role_name)[
        "passwordSecret"
    ]["name"]
    assert isinstance(actual_name, str)
    assert actual_name == secret


# Shared S3 configuration gate (recovery isolation slice).
#
# sharedS3.enabled defaults true and preserves every projection below. False
# omits the chart-owned R2 OnePasswordItem producer and all shared S3 consumer
# references in the backend and in job containers. False deletes nothing,
# revokes nothing, and stops nothing: it only stops this chart from
# provisioning or injecting the shared configuration. Unrelated jobs keep
# their schedules and must be disarmed separately with jobs.image "".
#
# Two chart-owned blocks are known defects and stay untouched here: the
# grimoire job credential block repeats secretKeyRef and names no Secret, and
# the .s3 job block hardcodes its Secret name. The tests below pin only env
# names, values, and credential keys around those blocks, never their
# validity, and the custom-release case keeps jobs disarmed.

_DEFAULT_S3_ENDPOINT = (
    "https://7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com"
)
_DEFAULT_S3_REGION = "auto"
_DEFAULT_ITEM_PATH = "vaults/k8s-homelab/items/r2-s3-credentials"

_SHARED_ENV = [
    "SEAWEEDFS_S3_ENDPOINT",
    "AWS_REGION",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
]
_S3_JOB_ENV = _SHARED_ENV + [
    "STARS_GRID_S3_BUCKET",
    "STARS_GRID_S3_KEY",
    "STARS_CLIMATOLOGY_S3_KEY",
    "STARS_SITES_S3_KEY",
    "STARS_MAP_S3_KEY",
    "STARS_HISTORY_MAP_S3_KEY",
]
_BACKEND_SHARED_ENV = _SHARED_ENV + [
    "STARS_GRID_S3_BUCKET",
    "STARS_GRID_S3_KEY",
    "STARS_CLIMATOLOGY_S3_KEY",
    "CHAT_BLOB_S3_BUCKET",
    "ARTIFACTS_S3_BUCKET",
    "ARTIFACT_PUBLIC_BASE",
]
_GRIMOIRE_OWN_ENV = [
    "EMBEDDING_URL",
    "EMBED_BATCH_READ_TIMEOUT",
    "EMBED_RETRY_TIMEOUT",
    "GRIMOIRE_EXTRACT_API_KEY",
    "GRIMOIRE_S3_BUCKET",
    "GRIMOIRE_EXTRACT_MODEL",
]
_SHARED_CREDENTIAL_KEYS = {"access-key-id", "secret-access-key"}


def _write_values(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def _deployment_backend_env(rendered):
    for doc in yaml.safe_load_all(rendered):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            containers = doc["spec"]["template"]["spec"]["containers"]
            return next(c for c in containers if c["name"] == "backend").get("env", [])
    raise AssertionError("no Deployment rendered; cannot assess shared S3 env")


def _cron_containers(rendered):
    out = []
    for doc in yaml.safe_load_all(rendered):
        if isinstance(doc, dict) and doc.get("kind") == "CronWorkflow":
            container = doc["spec"]["workflowSpec"]["templates"][0]["container"]
            out.append((doc["metadata"]["name"], container.get("env", [])))
    return out


def _cron_docs(rendered):
    return {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict) and doc.get("kind") == "CronWorkflow"
    }


def _r2_producers(rendered):
    return [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
        and doc.get("kind") == "OnePasswordItem"
        and doc.get("metadata", {}).get("name", "").endswith("-r2-s3")
    ]


def _secret_keys(env):
    keys = set()
    for item in env:
        ref = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
        if ref.get("key"):
            keys.add(ref["key"])
    return keys


def _env_by_name(env):
    out = {}
    for item in env:
        out.setdefault(item["name"], item)
    return out


def _shared_s3_projection(rendered):
    jobs = sorted(_cron_containers(rendered), key=lambda pair: pair[0])
    return (_r2_producers(rendered), _deployment_backend_env(rendered), jobs)


def test_shared_s3_default_preserves_complete_projection():
    rendered = _render("recovery", [])
    producers = _r2_producers(rendered)
    assert len(producers) == 1, "default gate must emit exactly one R2 producer"
    assert producers[0]["spec"]["itemPath"] == _DEFAULT_ITEM_PATH
    env = _deployment_backend_env(rendered)
    by_name = _env_by_name(env)
    names = [item["name"] for item in env]
    assert [n for n in names if n in set(_BACKEND_SHARED_ENV)] == _BACKEND_SHARED_ENV
    assert by_name["SEAWEEDFS_S3_ENDPOINT"].get("value") == _DEFAULT_S3_ENDPOINT
    assert by_name["AWS_REGION"].get("value") == _DEFAULT_S3_REGION
    for var, key in (
        ("S3_ACCESS_KEY_ID", "access-key-id"),
        ("S3_SECRET_ACCESS_KEY", "secret-access-key"),
    ):
        ref = by_name[var]["valueFrom"]["secretKeyRef"]
        assert ref["key"] == key
        assert ref["name"].endswith("-r2-s3")
    assert by_name["STARS_GRID_S3_BUCKET"].get("value") == "stars"
    assert by_name["STARS_GRID_S3_KEY"].get("value") == "grid.json"
    assert by_name["STARS_CLIMATOLOGY_S3_KEY"].get("value") == "climatology.json"
    assert by_name["CHAT_BLOB_S3_BUCKET"].get("value") == "chat"
    assert by_name["ARTIFACTS_S3_BUCKET"].get("value") == "artifacts"
    assert by_name["ARTIFACT_PUBLIC_BASE"].get("value") == "https://jomcgi.dev"
    grid = dict(_cron_containers(rendered)).get("stars-load-grid")
    assert grid is not None, "default .s3 job must render under the forced jobs image"
    grid_names = [item["name"] for item in grid]
    assert [n for n in grid_names if n in set(_S3_JOB_ENV)] == _S3_JOB_ENV


def test_shared_s3_explicit_true_matches_default(tmp_path):
    default_rendered = _render("recovery", [])
    override = _write_values(tmp_path, "s3-true.yaml", {"sharedS3": {"enabled": True}})
    true_rendered = _render("recovery", [override])
    assert _shared_s3_projection(true_rendered) == _shared_s3_projection(
        default_rendered
    )


@pytest.mark.parametrize("environment", ["prod", "gke"])
def test_home_and_gke_preserve_shared_s3_projection(environment, renders):
    rendered = renders[environment]
    assert _r2_producers(rendered), f"{environment} renders no R2 producer"
    names = [item["name"] for item in _deployment_backend_env(rendered)]
    assert [n for n in names if n in set(_BACKEND_SHARED_ENV)] == _BACKEND_SHARED_ENV


def test_shared_s3_false_omits_producer_and_all_consumer_references(tmp_path):
    override = _write_values(tmp_path, "s3-off.yaml", {"sharedS3": {"enabled": False}})
    rendered = _render("recovery", [override])
    assert _r2_producers(rendered) == []
    env = _deployment_backend_env(rendered)
    names = {item["name"] for item in env}
    assert not (set(_BACKEND_SHARED_ENV) & names)
    assert not (_SHARED_CREDENTIAL_KEYS & _secret_keys(env))
    assert "DATABASE_URL" in names
    jobs = _cron_containers(rendered)
    assert jobs, "no CronWorkflows rendered; omission proves nothing without jobs"
    for workflow, container_env in jobs:
        container_names = {item["name"] for item in container_env}
        assert not (set(_SHARED_ENV) & container_names), workflow
        assert not (_SHARED_CREDENTIAL_KEYS & _secret_keys(container_env)), workflow
    grid = dict(jobs).get("stars-load-grid")
    assert grid is not None, "the .s3 job stays scheduled; only its storage env goes"
    assert "DATABASE_URL" in {item["name"] for item in grid}


_PROBE_JOBS = {
    "grimoire": {"enabled": True},
    "jobs": {
        "cronWorkflows": [
            {
                "name": "probe-s3-job",
                "args": ["probe-s3"],
                "schedule": "0 * * * *",
                "concurrencyPolicy": "Forbid",
                "activeDeadlineSeconds": 120,
                "suspend": False,
                "replaces": "probe.s3_job",
                "s3": True,
                "env": {"PROBE_KEEP": "kept"},
            },
            {
                "name": "probe-grimoire-job",
                "args": ["probe-grimoire"],
                "schedule": "30 2 * * *",
                "concurrencyPolicy": "Forbid",
                "activeDeadlineSeconds": 120,
                "suspend": False,
                "grimoire": True,
                "env": {"PROBE_KEEP": "kept"},
            },
        ]
    },
}


def _probe_renders(tmp_path):
    base = _write_values(tmp_path, "probe.yaml", _PROBE_JOBS)
    off = _write_values(
        tmp_path, "probe-off.yaml", {"sharedS3": {"enabled": False}, **_PROBE_JOBS}
    )
    return _cron_docs(_render("recovery", [base])), _cron_docs(
        _render("recovery", [off])
    )


def test_shared_s3_probe_jobs_positive_then_omission(tmp_path):
    true_docs, false_docs = _probe_renders(tmp_path)
    assert set(true_docs) == {"probe-s3-job", "probe-grimoire-job"}
    assert set(false_docs) == {"probe-s3-job", "probe-grimoire-job"}
    s3_env = _env_by_name(
        true_docs["probe-s3-job"]["spec"]["workflowSpec"]["templates"][0]["container"][
            "env"
        ]
    )
    assert s3_env["SEAWEEDFS_S3_ENDPOINT"].get("value") == _DEFAULT_S3_ENDPOINT
    assert s3_env["AWS_REGION"].get("value") == _DEFAULT_S3_REGION
    assert s3_env["STARS_GRID_S3_BUCKET"].get("value") == "stars"
    assert s3_env["STARS_SITES_S3_KEY"].get("value") == "sites.json"
    assert s3_env["PROBE_KEEP"].get("value") == "kept"
    assert "DATABASE_URL" in s3_env
    grimoire_env = _env_by_name(
        true_docs["probe-grimoire-job"]["spec"]["workflowSpec"]["templates"][0][
            "container"
        ]["env"]
    )
    for name in _SHARED_ENV + _GRIMOIRE_OWN_ENV:
        assert name in grimoire_env, f"positive grimoire control lacks {name}"
    for name, gated in (
        ("probe-s3-job", _S3_JOB_ENV),
        ("probe-grimoire-job", _SHARED_ENV),
    ):
        get_env = lambda doc: doc["spec"]["workflowSpec"]["templates"][0]["container"][
            "env"
        ]  # noqa: E731
        true_env = {i["name"]: i for i in get_env(true_docs[name])}
        false_env = {i["name"]: i for i in get_env(false_docs[name])}
        assert not (set(gated) & set(false_env)), name
        assert {k: v for k, v in true_env.items() if k not in gated} == {
            k: v for k, v in false_env.items() if k not in gated
        }, name
        assert (
            true_docs[name]["spec"]["schedules"]
            == false_docs[name]["spec"]["schedules"]
        )
        assert (
            true_docs[name]["spec"]["workflowSpec"]["templates"][0]["container"]["args"]
            == false_docs[name]["spec"]["workflowSpec"]["templates"][0]["container"][
                "args"
            ]
        )
        assert true_docs[name]["metadata"].get("annotations") == false_docs[name][
            "metadata"
        ].get("annotations")
    for name in ("probe-s3-job", "probe-grimoire-job"):
        assert "PROBE_KEEP" in {
            i["name"]
            for i in false_docs[name]["spec"]["workflowSpec"]["templates"][0][
                "container"
            ]["env"]
        }
    for name in _GRIMOIRE_OWN_ENV:
        assert name in {
            i["name"]
            for i in false_docs["probe-grimoire-job"]["spec"]["workflowSpec"][
                "templates"
            ][0]["container"]["env"]
        }, f"grimoire-owned {name} must survive the shared gate"


def test_shared_s3_false_disarmed_recovery_release(tmp_path):
    override = _write_values(
        tmp_path,
        "recovery.yaml",
        {"sharedS3": {"enabled": False}, "jobs": {"image": ""}},
    )
    rendered = _render("recovery", [override])
    assert _r2_producers(rendered) == []
    env = _deployment_backend_env(rendered)
    names = {item["name"] for item in env}
    assert not (set(_BACKEND_SHARED_ENV) & names)
    assert not (_SHARED_CREDENTIAL_KEYS & _secret_keys(env))
    assert "CronWorkflow" not in [kind for kind, _n, _d in _docs(rendered)]
    db = _env_by_name(env)["DATABASE_URL"]["valueFrom"]["secretKeyRef"]
    assert db["key"] == "uri" and db["name"].endswith("-pg-app")
    deployments = [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict) and doc.get("kind") == "Deployment"
    ]
    assert deployments, "no Deployment rendered"
    progress = [
        c
        for c in deployments[0]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "progress-ingest"
    ]
    assert progress and "DATABASE_URL" in {
        i["name"] for i in progress[0].get("env", [])
    }
    kinds = {(k, n) for k, n, _d in _docs(rendered)}
    assert any(k == "ClusterRole" for k, _n in kinds)


def test_shared_s3_false_with_cleared_item_path_still_omits(tmp_path):
    override = _write_values(
        tmp_path,
        "s3-off-cleared.yaml",
        {"sharedS3": {"enabled": False}, "stars": {"onepassword": {"itemPath": ""}}},
    )
    rendered = _render("recovery", [override])
    assert _r2_producers(rendered) == []
    names = {item["name"] for item in _deployment_backend_env(rendered)}
    assert not (set(_BACKEND_SHARED_ENV) & names)


def test_shared_s3_true_with_cleared_item_path_keeps_external_consumers(tmp_path):
    override = _write_values(
        tmp_path,
        "s3-on-cleared.yaml",
        {"sharedS3": {"enabled": True}, "stars": {"onepassword": {"itemPath": ""}}},
    )
    rendered = _render("recovery", [override])
    assert _r2_producers(rendered) == []
    by_name = _env_by_name(_deployment_backend_env(rendered))
    assert by_name["SEAWEEDFS_S3_ENDPOINT"].get("value") == _DEFAULT_S3_ENDPOINT
    assert by_name["AWS_REGION"].get("value") == _DEFAULT_S3_REGION
    ref = by_name["S3_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "access-key-id" and ref["name"].endswith("-r2-s3")


@pytest.mark.parametrize(
    "enabled",
    ["false", "true", 1, 0, None, [True]],
    ids=["quoted-false", "quoted-true", "int-one", "int-zero", "null", "list"],
)
def test_shared_s3_enabled_rejects_non_boolean(tmp_path, enabled):
    override = _write_values(tmp_path, "bad.yaml", {"sharedS3": {"enabled": enabled}})
    with pytest.raises(RuntimeError, match=r"sharedS3\.enabled must be a boolean"):
        _render("recovery", [override])


@pytest.mark.parametrize(
    "gate",
    [None, "false", 1, [True]],
    ids=["null", "string", "int", "list"],
)
def test_shared_s3_rejects_non_map(tmp_path, gate):
    override = _write_values(tmp_path, "bad.yaml", {"sharedS3": gate})
    with pytest.raises(RuntimeError, match=r"sharedS3 must be a map"):
        _render("recovery", [override])


_NOTIFICATION_ENV = {
    "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID",
    "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID",
    "MONOLITH_AGENT_DISCORD_AGENT_SESSIONS_CHANNEL_ID",
    "AGENT_SESSIONS_CHANNEL_NOTIFY",
    "OWNER_DISCORD_USER_ID",
}


def _notification_projection(rendered):
    return [
        item
        for item in _deployment_backend_env(rendered)
        if item["name"] in _NOTIFICATION_ENV
    ]


def _chat_secret_producers(rendered):
    return [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
        and doc.get("kind") == "OnePasswordItem"
        and doc["metadata"]["name"].endswith("-chat-secrets")
    ]


@pytest.mark.parametrize("policy", [None, "none"])
def test_session_notification_policy_without_discord_configuration(tmp_path, policy):
    overrides = []
    if policy is not None:
        overrides.append(
            _write_values(
                tmp_path,
                "notification-policy.yaml",
                {"agents": {"sessions": {"channelNotify": policy}}},
            )
        )
    rendered = _render("recovery-notify", overrides)
    assert _notification_projection(rendered) == [
        {"name": "AGENT_SESSIONS_CHANNEL_NOTIFY", "value": policy or "needs-input"}
    ]
    assert _chat_secret_producers(rendered) == []


@pytest.mark.parametrize("chat_enabled", [False, True])
@pytest.mark.parametrize("sessions_channel", [None, "sessions-channel"])
def test_discord_owner_reference_follows_chat_producer(
    tmp_path, chat_enabled, sessions_channel
):
    discord = {"defaultServerId": "test-server", "defaultChannelId": "test-channel"}
    expected = [
        {"name": "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "value": "test-server"},
        {"name": "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "value": "test-channel"},
    ]
    if sessions_channel is not None:
        discord["agentSessionsChannelId"] = sessions_channel
        expected.append(
            {
                "name": "MONOLITH_AGENT_DISCORD_AGENT_SESSIONS_CHANNEL_ID",
                "value": sessions_channel,
            }
        )
    expected.append({"name": "AGENT_SESSIONS_CHANNEL_NOTIFY", "value": "none"})
    if chat_enabled:
        expected.append(
            {
                "name": "OWNER_DISCORD_USER_ID",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "recovery-notify-chat-secrets",
                        "key": "OWNER_DISCORD_USER_ID",
                    }
                },
            }
        )
    override = _write_values(
        tmp_path,
        "notification-owner.yaml",
        {
            "agent": {"discord": discord},
            "agents": {"sessions": {"channelNotify": "none"}},
            "chat": {"enabled": chat_enabled},
        },
    )
    rendered = _render("recovery-notify", [override])
    assert _notification_projection(rendered) == expected
    producers = _chat_secret_producers(rendered)
    if chat_enabled:
        assert len(producers) == 1
        assert producers[0]["metadata"]["name"] == "recovery-notify-chat-secrets"
        assert producers[0]["metadata"]["namespace"] == "recovery-notify"
    else:
        assert producers == []


@pytest.mark.parametrize("environment", ["prod", "gke"])
def test_production_notification_projection_is_preserved(environment, renders):
    rendered = renders[environment]
    assert _notification_projection(rendered) == [
        {
            "name": "MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID",
            "value": "1501965852042330302",
        },
        {
            "name": "MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID",
            "value": "1501965852969402517",
        },
        {
            "name": "MONOLITH_AGENT_DISCORD_AGENT_SESSIONS_CHANNEL_ID",
            "value": "1533337680463663254",
        },
        {"name": "AGENT_SESSIONS_CHANNEL_NOTIFY", "value": "needs-input"},
        {
            "name": "OWNER_DISCORD_USER_ID",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "monolith-chat-secrets",
                    "key": "OWNER_DISCORD_USER_ID",
                }
            },
        },
    ]
    producers = _chat_secret_producers(rendered)
    assert len(producers) == 1
    assert producers[0]["metadata"]["name"] == "monolith-chat-secrets"


def _startup_control_env(rendered):
    entries = _deployment_backend_env(rendered)
    for name in (
        "CD_PROBE_ENABLED",
        "HOME_OBSERVABILITY_PRIME_ENABLED",
        "MONOLITH_LEADER_SINGLETONS",
    ):
        assert sum(item["name"] == name for item in entries) == 1
    return _env_by_name(entries)


@pytest.mark.parametrize("probe_enabled", [False, True])
@pytest.mark.parametrize("prime_enabled", [False, True])
def test_recovery_startup_controls_preserve_leader_election(
    tmp_path, probe_enabled, prime_enabled
):
    override = _write_values(
        tmp_path,
        "startup-controls.yaml",
        {
            "cdHealth": {"probeEnabled": probe_enabled},
            "backend": {"primeObservabilitySnapshots": prime_enabled},
        },
    )
    env = _startup_control_env(_render("recovery", [override]))
    assert env["CD_PROBE_ENABLED"]["value"] == str(probe_enabled).lower()
    assert (
        env["HOME_OBSERVABILITY_PRIME_ENABLED"]["value"] == str(prime_enabled).lower()
    )
    assert env["MONOLITH_LEADER_SINGLETONS"]["value"] == "true"
    assert env["CD_PROBE_INTERVAL_S"]["value"] == "300"


def test_default_startup_controls_remain_enabled():
    env = _startup_control_env(_render("recovery", []))
    assert env["CD_PROBE_ENABLED"]["value"] == "true"
    assert env["HOME_OBSERVABILITY_PRIME_ENABLED"]["value"] == "true"
    assert env["MONOLITH_LEADER_SINGLETONS"]["value"] == "true"


@pytest.mark.parametrize("tier", ["prod", "gke"])
def test_deployed_startup_controls_remain_enabled(renders, tier):
    env = _startup_control_env(renders[tier])
    assert env["CD_PROBE_ENABLED"]["value"] == "true"
    assert env["HOME_OBSERVABILITY_PRIME_ENABLED"]["value"] == "true"
    assert env["MONOLITH_LEADER_SINGLETONS"]["value"] == "true"


@pytest.fixture(scope="module")
def recovery_gke_render():
    # Do not inherit production's restored DB or historical dev's leader mute.
    return _render("monolith-dev", [Path(os.environ["RECOVERY_GKE_VALUES"])])


def test_recovery_gke_has_no_shared_control_or_external_owners(recovery_gke_render):
    forbidden = CLUSTER_SCOPED | {
        "Role",
        "RoleBinding",
        "CronWorkflow",
        "CronJob",
        "ScheduledBackup",
        "HTTPRoute",
        "SecurityPolicy",
        "OnePasswordItem",
        "CiliumNetworkPolicy",
        "HorizontalPodAutoscaler",
    }
    for kind, name, document in _docs(recovery_gke_render):
        assert kind not in forbidden, (kind, name)
        assert _pinned_namespace(document) in {None, "monolith-dev"}, (kind, name)
    assert _r2_producers(recovery_gke_render) == []


def test_recovery_gke_uses_fresh_database_and_local_consumers(recovery_gke_render):
    documents = [
        d for d in yaml.safe_load_all(recovery_gke_render) if isinstance(d, dict)
    ]
    cluster = _cnpg_cluster(documents)
    assert cluster["metadata"]["name"] == "monolith-dev-pg"
    spec = cluster["spec"]
    assert spec["instances"] == 1
    assert spec["storage"] == {"size": "10Gi", "storageClass": "standard-rwo"}
    assert spec["bootstrap"] == {
        "initdb": {
            "database": "monolith",
            "owner": "app",
            "postInitSQL": ["CREATE EXTENSION IF NOT EXISTS vector"],
        }
    }
    assert "externalClusters" not in spec
    assert "backup" not in spec
    roles = {r["name"]: r for r in spec["managed"]["roles"]}
    assert set(roles) == {"public_reader", "agents_writer", "public_writer", "embervm"}
    for name in ("public_reader", "agents_writer", "public_writer"):
        assert roles[name]["login"] is False
        assert "passwordSecret" not in roles[name]
    assert roles["embervm"]["passwordSecret"] == {
        "name": "monolith-dev-pg-embervm-oplog"
    }
    database = next(d for d in documents if d.get("kind") == "Database")
    assert database["spec"] == {
        "cluster": {"name": "monolith-dev-pg"},
        "name": "embervm_oplog",
        "owner": "embervm",
        "ensure": "present",
        "databaseReclaimPolicy": "retain",
    }
    deployment = next(d for d in documents if d.get("kind") == "Deployment")
    expected_ref = {"name": "monolith-dev-pg-app", "key": "uri"}
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container["name"] in {"backend", "progress-ingest"}:
            env = _env_by_name(container["env"])
            assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == expected_ref
    migrations = [d for d in documents if d.get("kind") == "AtlasMigration"]
    assert len(migrations) == 1
    assert migrations[0]["spec"]["urlFrom"] == {"secretKeyRef": expected_ref}


def test_recovery_gke_keeps_real_failover_and_bounded_drainer(recovery_gke_render):
    documents = [
        d for d in yaml.safe_load_all(recovery_gke_render) if isinstance(d, dict)
    ]
    deployments = [d for d in documents if d.get("kind") == "Deployment"]
    assert len(deployments) == 1
    deployment = deployments[0]
    assert deployment["metadata"]["name"] == "monolith-dev"
    assert deployment["spec"]["replicas"] == 2
    assert (
        deployment["spec"]["template"]["spec"]["serviceAccountName"] == "monolith-dev"
    )
    env = _env_by_name(_deployment_backend_env(recovery_gke_render))
    expected = {
        "MONOLITH_LEADER_SINGLETONS": "true",
        "HOME_OBSERVABILITY_PRIME_ENABLED": "false",
        "CD_PROBE_ENABLED": "false",
        "DRAINER_ENABLED": "true",
        "DRAINER_JOB_KINDS": "qwen-drain",
        "DRAINER_MAX_JOBS_PER_CYCLE": "1",
        "DRAINER_NOTIFY_FAILURES": "false",
        "DRAINER_DOCFIX_ENABLED": "false",
        "DRAINER_DOCFIX_AUTO_MERGE": "false",
        "AGENT_SESSIONS_CHANNEL_NOTIFY": "none",
        "AGENT_MODELS": "luna",
        "SCHEDULER_WORKFLOW_NAMESPACE": "monolith-dev",
    }
    for name, value in expected.items():
        assert env[name]["value"] == value, name
    assert "SWARM_ENABLED" not in env
    default_jobs = yaml.safe_load((_chart_dir() / "values.yaml").read_text())["jobs"]
    replaced = {
        j["replaces"] for j in default_jobs["cronWorkflows"] if j.get("replaces")
    }
    assert replaced
    assert set(filter(None, env["ARGO_JOBS"]["value"].split(","))) == replaced


def test_recovery_gke_session_dependencies_use_dev_identities(recovery_gke_render):
    env = _env_by_name(_deployment_backend_env(recovery_gke_render))
    expected = {
        "EMBERVM_URL": "http://embervm-dev-embervm.embervm-dev:8080",
        "EMBER_TOKENBROKER_URL": "http://embervm-dev-embervm-tokenbroker.embervm-dev:8080",
        "SANDBOX_WORKLOAD_PREFIX": "sandbox-dev-",
        "AUTH_AUTHENTIK_JWKS_URL": "https://auth.jomcgi.dev/application/o/mcp-recovery/jwks/",
        "AUTH_AUTHENTIK_ISSUER": "https://auth.jomcgi.dev/application/o/mcp-recovery/",
        "AUTH_AUTHENTIK_AUDIENCE": "https://factory-recovery.jomcgi.dev",
        "AUTH_AUTHENTIK_AGENT_JWKS_URL": "https://auth.jomcgi.dev/application/o/mcp-recovery/jwks/",
        "AUTH_AUTHENTIK_AGENT_ISSUER": "https://auth.jomcgi.dev/application/o/mcp-recovery/",
        "AUTH_AUTHENTIK_AGENT_AUDIENCE": "https://factory-recovery.jomcgi.dev",
    }
    for name, value in expected.items():
        assert env[name]["value"] == value, name
    assert not (
        {"DISCORD_BOT_TOKEN", "OWNER_DISCORD_USER_ID", "AISSTREAM_API_KEY"} & env.keys()
    )
    assert not (set(_BACKEND_SHARED_ENV) & env.keys())
    assert not (_SHARED_CREDENTIAL_KEYS & _secret_keys(list(env.values())))
    assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in env
