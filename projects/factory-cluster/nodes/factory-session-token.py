#!/usr/bin/python3 -I
"""Mint the node-1 kubectl credential. Install and run this helper as root."""

import json
import socket
import subprocess
import sys

if len(sys.argv) != 1 or socket.gethostname().split(".")[0] != "node-1":
    sys.exit("factory session credentials are available only on node-1")

request = {
    "apiVersion": "authentication.k8s.io/v1",
    "kind": "TokenRequest",
    "spec": {"expirationSeconds": 600},
}

try:
    result = subprocess.run(
        [
            "/usr/local/bin/k3s",
            "kubectl",
            "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
            "--server=https://127.0.0.1:6443",
            "--request-timeout=10s",
            "create",
            "--raw=/api/v1/namespaces/kube-system/serviceaccounts/factory-node1-sessions/token",
            "-f",
            "-",
        ],
        input=json.dumps(request),
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        env={"HOME": "/root", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        timeout=15,
    )
    issued = json.loads(result.stdout)["status"]
    status = {
        "token": issued["token"],
        "expirationTimestamp": issued["expirationTimestamp"],
    }
    if not all(isinstance(value, str) and value for value in status.values()):
        sys.exit("k3s returned an incomplete factory session credential")
except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
    sys.exit("k3s could not issue the factory session credential")

print(
    json.dumps(
        {
            "apiVersion": "client.authentication.k8s.io/v1",
            "kind": "ExecCredential",
            "status": status,
        }
    )
)
