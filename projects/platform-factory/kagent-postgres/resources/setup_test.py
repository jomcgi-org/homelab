from pathlib import Path

import yaml

ROOT = Path(__file__).parent


def test_setup_uses_only_application_credentials():
    cluster, database, *_ = yaml.safe_load_all((ROOT / "cluster.yaml").read_text())
    assert cluster["spec"]["enableSuperuserAccess"] is False
    assert database["kind"] == "Database"
    assert database["spec"]["owner"] == cluster["spec"]["bootstrap"]["initdb"]["owner"]
    assert database["spec"]["schemas"] == [
        {"name": "kagent", "owner": "kagent_owner", "ensure": "present"}
    ]
    resources = list(yaml.safe_load_all((ROOT / "setup.yaml").read_text()))
    job = next(
        r
        for r in resources
        if r["kind"] == "Job" and r["metadata"]["name"] == "kagent-pg-setup"
    )
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {
        v["valueFrom"]["secretKeyRef"]["name"] for v in env if "valueFrom" in v
    } == {
        "kagent-pg-logins",
    }
    roles = {r["name"]: r for r in cluster["spec"]["managed"]["roles"]}
    for access in ("owner", "readwrite"):
        group = f"kagent_{access}"
        login = roles[f"{group}_user"]
        assert roles[group]["login"] is False
        assert login["login"] and not login["inherit"]
        assert login["inRoles"] == [group]
        secret = next(
            r
            for r in resources
            if r["metadata"]["name"] == login["passwordSecret"]["name"]
        )
        assert secret["type"] == "kubernetes.io/basic-auth"
        assert secret["stringData"]["username"] == login["name"]
