"""Apply resources/bank-template.json to the factory's bank on every sync.

Hindsight applies HINDSIGHT_API_DEFAULT_BANK_TEMPLATE only when it creates a
bank. This PostSync Job (hindsight.yaml) creates BANK when it is missing,
imports the template into it, and reads the config back. So the bank carries
the template in git, also after the template changes, and also when the bank
existed before. An import changes the bank's config, never its memories, and
importing the same template again changes nothing.

Uses only the standard library. The Service's address comes from the service
links Kubernetes sets for the namespace, so the Job needs no DNS.
"""

import json
import os
import time
import urllib.error
import urllib.request

TEMPLATE = "/template/bank-template.json"


def call(base, key, method, path, body=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


def labels(groups):
    """Entity label groups without the defaults Hindsight fills in."""
    return [
        {
            "key": g["key"],
            "type": g.get("type", "value"),
            "tag": g.get("tag", False),
            "optional": g.get("optional", True),
            "values": [v["value"] for v in g.get("values", [])],
        }
        for g in groups or []
    ]


def mismatches(config, want):
    """The template settings the bank's config does not carry."""
    out = []
    for name, value in want.items():
        if name == "entity_labels":
            if labels(config.get(name)) != labels(value):
                out.append(name)
        elif config.get(name) != value:
            out.append(name)
    return out


def apply(call, bank, template, wait=10, tries=60):
    for _ in range(tries):
        status, _ = call("GET", "/health")
        if status == 200:
            break
        time.sleep(wait)
    else:
        raise SystemExit("Hindsight did not become ready")
    status, body = call("GET", "/v1/default/banks?limit=1000")
    if status != 200:
        raise SystemExit(f"list banks: {status} {body}")
    if bank not in {b.get("bank_id") for b in body["banks"]}:
        status, body = call("PUT", f"/v1/default/banks/{bank}", {})
        if status != 200:
            raise SystemExit(f"create bank {bank}: {status} {body}")
        print(f"created bank {bank}")
    status, body = call("POST", f"/v1/default/banks/{bank}/import", template)
    if status != 200 or not body.get("config_applied"):
        raise SystemExit(f"import into {bank}: {status} {body}")
    status, body = call("GET", f"/v1/default/banks/{bank}/config")
    if status != 200:
        raise SystemExit(f"read {bank} config: {status} {body}")
    missing = mismatches(body["config"], template["bank"])
    if missing:
        raise SystemExit(f"{bank} config differs from the template: {missing}")
    print(f"{bank} carries the template: {sorted(template['bank'])}")


def main():
    host = os.environ["HINDSIGHT_API_SERVICE_HOST"]
    base = f"http://{host}:{os.environ['HINDSIGHT_API_SERVICE_PORT']}"
    key = os.environ["TENANT_API_KEY"]
    with open(TEMPLATE) as f:
        template = json.load(f)
    apply(lambda *args: call(base, key, *args), os.environ["BANK"], template)


if __name__ == "__main__":
    main()
